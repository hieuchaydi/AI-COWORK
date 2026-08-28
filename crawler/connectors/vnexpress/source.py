"""
VnExpress Source Connector.
Handles RSS and Sitemap discovery, and extraction based on rules.
"""

import re
import datetime
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel


class ArticleRecord(BaseModel):
    title: Optional[str] = None
    lead: Optional[str] = None
    published_at: Optional[datetime.datetime] = None
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    authors: Optional[str] = None
    url: str


class VnExpressSource:
    """
    VnExpress connector for crawler.
    """
    
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        
    async def discover_rss(self, feed_urls: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch RSS and parse <item> tags.
        """
        tasks = []
        for url in feed_urls:
            try:
                response = await self.client.get(url)
                response.raise_for_status()
                root = ET.fromstring(response.content)
                for item in root.findall('.//item'):
                    title = item.findtext('title')
                    link = item.findtext('link')
                    pub_date = item.findtext('pubDate')
                    description = item.findtext('description')
                    if link:
                        tasks.append({
                            'url': link,
                            'title': title,
                            'pub_date': pub_date,
                            'description': description
                        })
            except Exception as e:
                # Log error in real implementation
                pass
        return tasks

    async def discover_sitemap(self, sitemap_url: str, since_lastmod: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
        """
        Parse sitemap index and child sitemaps.
        """
        tasks = []
        try:
            response = await self.client.get(sitemap_url)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            # Basic parsing of sitemap index or sitemap
            namespaces = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
            for loc in root.findall('.//sm:loc', namespaces):
                child_url = loc.text
                if child_url:
                    if child_url.endswith('.xml'):
                        # child sitemap
                        child_tasks = await self.discover_sitemap(child_url, since_lastmod)
                        tasks.extend(child_tasks)
                    else:
                        tasks.append({'url': child_url})
        except Exception as e:
            pass
        return tasks

    def extract(self, html: str, url: str = "", fetched_at: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """
        Run extraction rules -> ArticleRecord dict
        """
        soup = BeautifulSoup(html, 'html.parser')
        
        record: Dict[str, Any] = {'url': url}
        
        title_meta = soup.find('meta', property='og:title')
        if title_meta:
            record['title'] = title_meta.get('content')
        else:
            h1 = soup.select_one('h1.title-detail')
            if h1:
                record['title'] = h1.get_text(strip=True)
                
        lead_meta = soup.find('meta', property='og:description')
        if lead_meta:
            record['lead'] = lead_meta.get('content')
            
        pub_meta = soup.find('meta', property='article:published_time')
        if pub_meta:
            record['published_at'] = pub_meta.get('content')
            
        body = soup.select_one('article.fck_detail')
        if body:
            for selector in ['script', 'style', '.box-tinlienquan', 'figure.tplCaption > figcaption', '.box-comment']:
                for el in body.select(selector):
                    el.decompose()
            record['body_html'] = str(body)
            record['body_text'] = body.get_text(separator='\n', strip=True)
            
        authors = soup.select('p.Normal strong')
        if authors:
            record['authors'] = ", ".join([a.get_text(strip=True) for a in authors])
            
        return record
