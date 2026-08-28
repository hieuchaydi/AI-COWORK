"""Declarative extractor engine.

Extractors are DATA, not code, wherever possible. A YAML rules file defines
how to extract each field with a fallback chain:
  1. JSON-LD (structured data)
  2. Meta tags (og:*, article:*)
  3. CSS/XPath selectors (last resort)

Sites redesign their DOM far more often than they change their JSON-LD or
OpenGraph tags — this ordering is the single biggest driver of crawler lifespan.

Extraction is a PURE FUNCTION of FetchResult — no network, no clock, no randomness.
This is what makes golden-file tests possible.
"""

import json
import re
from typing import Any, Optional, Dict, List, Tuple, Set
from pathlib import Path
import yaml
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

class FieldRule:
    """One step in a field's fallback chain."""
    def __init__(self, source: str, selector: str, mode: str = "text", 
                 remove: List[str] | None = None, transforms: List[str] | None = None):
        self.source = source
        self.selector = selector
        self.mode = mode
        self.remove = remove or []
        self.transforms = transforms or []

class ExtractionRules:
    """Parsed extraction rules from YAML."""
    def __init__(self, record_type: str, version: int, fields: Dict[str, List[FieldRule]], required_fields: Set[str]):
        self.record_type = record_type
        self.version = version
        self.fields = fields
        self.required_fields = required_fields

class ExtractionError(Exception):
    """Raised when extraction fails critically, e.g. missing required fields."""
    pass

def load_rules(path: str | Path) -> ExtractionRules:
    """Load extraction rules from YAML file."""
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    
    fields = {}
    for field_name, rule_list in data.get('fields', {}).items():
        rules = []
        for r in rule_list:
            rules.append(FieldRule(
                source=r.get('source'),
                selector=r.get('selector', r.get('key')),
                mode=r.get('mode', 'text'),
                remove=r.get('remove'),
                transforms=r.get('transforms')
            ))
        fields[field_name] = rules
        
    return ExtractionRules(
        record_type=data.get('record_type', 'Unknown'),
        version=data.get('version', 1),
        fields=fields,
        required_fields=set(data.get('required_fields', []))
    )

def extract_jsonld(soup: BeautifulSoup, schema_type: str, field: str) -> Optional[Any]:
    """Extract a field from JSON-LD structured data.
    Looks for <script type='application/ld+json'> containing the schema_type."""
    scripts = soup.find_all('script', type='application/ld+json')
    for script in scripts:
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                if item.get('@type') == schema_type or schema_type == '*':
                    if field in item:
                        return item[field]
        except (json.JSONDecodeError, TypeError):
            continue
    return None

def extract_meta(soup: BeautifulSoup, name: str) -> Optional[str]:
    """Extract from <meta> tags. Supports:
    - og:title → <meta property='og:title'>
    - article:published_time → <meta property='article:published_time'>
    - description → <meta name='description'>"""
    if name.startswith('og:') or name.startswith('article:'):
        tag = soup.find('meta', property=name)
    else:
        tag = soup.find('meta', attrs={'name': name})
    
    if tag and hasattr(tag, 'attrs') and 'content' in tag.attrs:
        return tag.attrs['content']
    return None

def extract_css(soup: BeautifulSoup, selector: str, mode: str = "text",
                remove: List[str] | None = None) -> Optional[str]:
    """Extract using CSS selector. mode='text' for visible text, 'html' for innerHTML.
    remove: list of selectors to decompose before extraction."""
    import copy
    element = soup.select_one(selector)
    if not element:
        return None
    
    if remove:
        element = copy.copy(element)
        for rem_sel in remove:
            for rem_el in element.select(rem_sel):
                rem_el.decompose()
                
    if mode == 'html':
        return "".join(str(c) for c in element.contents).strip()
    return element.get_text(separator=' ', strip=True)

def apply_transforms(value: Any, transforms: List[str]) -> Any:
    if not transforms or value is None:
        return value
    
    for t in transforms:
        if t == 'vn_datetime' and isinstance(value, str):
            value = parse_vn_datetime(value)
        elif t == 'iso_datetime' and isinstance(value, str):
            value = parse_iso_datetime(value)
        elif t == 'strip_html' and isinstance(value, str):
            value = BeautifulSoup(value, 'html.parser').get_text(separator=' ', strip=True)
    return value

def extract_field(soup: BeautifulSoup, rules: List[FieldRule]) -> Optional[Any]:
    """Try each rule in the fallback chain until one succeeds."""
    for rule in rules:
        val = None
        if rule.source == 'jsonld':
            parts = rule.selector.split('.')
            schema_type = parts[0] if len(parts) > 1 else '*'
            field = parts[1] if len(parts) > 1 else rule.selector
            val = extract_jsonld(soup, schema_type, field)
        elif rule.source == 'meta':
            val = extract_meta(soup, rule.selector)
        elif rule.source == 'css':
            val = extract_css(soup, rule.selector, rule.mode, rule.remove)
            
        if val is not None:
            return apply_transforms(val, rule.transforms)
    return None

def extract_record(html: str, rules: ExtractionRules, fetched_at: datetime) -> Tuple[Dict[str, Any], List[str]]:
    """Extract a complete record from HTML.
    Returns: (field_dict, list_of_warnings)
    Raises: ExtractionError if required fields are missing."""
    soup = BeautifulSoup(html, 'html.parser')
    record = {}
    warnings = []
    
    for field_name, field_rules in rules.fields.items():
        val = extract_field(soup, field_rules)
        if val is not None:
            record[field_name] = val
        else:
            if field_name in rules.required_fields:
                warnings.append(f"Required field missing: {field_name}")
            else:
                warnings.append(f"Optional field missing: {field_name}")
            
    missing_required = [w for w in warnings if w.startswith("Required")]
    if missing_required:
        raise ExtractionError(f"Missing required fields: {', '.join(missing_required)}")
        
    record['fetched_at'] = fetched_at
    record['extractor_version'] = rules.version
    
    return record, warnings

def parse_vn_datetime(text: str) -> Optional[str]:
    """Parse Vietnamese datetime formats:
    - 'Thứ năm, 28/8/2026, 14:30 (GMT+7)'
    - '28/08/2026 14:30'
    - '28-08-2026'
    Returns ISO 8601 string with +07:00 offset."""
    match = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})[^\d]*(\d{1,2})[:h](\d{1,2})', text)
    if match:
        day, month, year, hour, minute = map(int, match.groups())
        dt = datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=7)))
        return dt.isoformat()
    
    match = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', text)
    if match:
        day, month, year = map(int, match.groups())
        dt = datetime(year, month, day, tzinfo=timezone(timedelta(hours=7)))
        return dt.isoformat()
        
    return None

def parse_iso_datetime(text: str) -> Optional[str]:
    """Parse ISO 8601 datetime, normalize to UTC."""
    try:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None
