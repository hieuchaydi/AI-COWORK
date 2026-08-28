"""
URL canonicalization and identity functions.
"""
import re
import hashlib
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from typing import Optional
from .models import IdentityConfig

# Tracking parameters to strip
TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'fbclid', 'gclid', 'source', 'ref'
}

def canonicalize_url(url: str) -> str:
    """
    Normalizes a URL by:
    - Lowercasing the host
    - Stripping the fragment
    - Stripping common tracking parameters
    """
    parsed = urlparse(url)
    
    # Lowercase host
    netloc = parsed.netloc.lower()
    
    # Strip tracking params
    query = parsed.query
    if query:
        params = parse_qsl(query, keep_blank_values=True)
        filtered_params = [(k, v) for k, v in params if k.lower() not in TRACKING_PARAMS]
        query = urlencode(filtered_params)
    
    # Strip fragment (set to empty)
    # Reconstruct URL
    canonical_parsed = parsed._replace(netloc=netloc, query=query, fragment="")
    
    return urlunparse(canonical_parsed)

def extract_id(url: str, pattern: str) -> Optional[str]:
    """
    Extracts an ID from a URL using a regex pattern.
    Assumes the first capture group contains the ID.
    """
    match = re.search(pattern, url)
    if match and match.groups():
        return match.group(1)
    return None

def compute_dedup_key(url: str, identity_config: Optional[IdentityConfig] = None) -> str:
    """
    Computes a stable deduplication key for a URL.
    Uses canonical URL if no identity config, else uses extracted ID.
    """
    canonical = canonicalize_url(url)
    if identity_config and identity_config.id_regex:
        extracted = extract_id(canonical, identity_config.id_regex)
        if extracted:
            return f"id:{extracted}"
    return f"url:{canonical}"

def compute_content_hash(title: str, body_text: str) -> str:
    """
    Computes a SHA-256 hash of the content (title + body).
    """
    content = f"{title}\n\n{body_text}".encode('utf-8')
    return hashlib.sha256(content).hexdigest()
