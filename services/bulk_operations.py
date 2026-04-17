"""
Bulk database operations for high-performance article storage.
Reduces DB queries from thousands to just a few batch operations.
"""

import hashlib
import logging
from typing import Dict, List, Any, Optional, Tuple
from functools import lru_cache
from threading import Lock
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# =============================================================================
# CATEGORY CACHE
# =============================================================================

class CategoryCache:
    """
    In-memory cache for categories and their keywords.
    Avoids repeated DB lookups for every article.
    """
    
    _instance = None
    _lock = Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        self.categories_by_slug: Dict[str, Any] = {}
        self.categories_by_id: Dict[int, Any] = {}
        self.keyword_to_categories: Dict[str, List[Any]] = defaultdict(list)
        self._loaded = False
        self.lock = Lock()
    
    def load(self, force_reload: bool = False):
        """Load categories from database into cache."""
        if self._loaded and not force_reload:
            return
        
        from web_app.models import Category
        
        with self.lock:
            if self._loaded and not force_reload:
                return
            
            self.categories_by_slug.clear()
            self.categories_by_id.clear()
            self.keyword_to_categories.clear()
            
            categories = Category.objects.filter(is_active=True)
            
            for cat in categories:
                self.categories_by_slug[cat.slug] = cat
                self.categories_by_id[cat.pk] = cat
                
                # Index keywords for fast lookup
                keywords = cat.keywords if cat.keywords else []
                for keyword in keywords:
                    kw_lower = keyword.lower().strip()
                    if kw_lower:
                        self.keyword_to_categories[kw_lower].append(cat)
            
            self._loaded = True
            logger.info(f"Loaded {len(self.categories_by_slug)} categories with {len(self.keyword_to_categories)} keywords")
    
    def get_by_slug(self, slug: str) -> Optional[Any]:
        """Get category by slug."""
        self.load()
        return self.categories_by_slug.get(slug)
    
    def get_or_create_by_slug(self, slug: str, name: str) -> Tuple[Any, bool]:
        """Get or create category by slug."""
        self.load()
        
        cat = self.categories_by_slug.get(slug)
        if cat:
            return cat, False
        
        # Create new category
        from web_app.models import Category
        
        with self.lock:
            # Double-check after lock
            cat = self.categories_by_slug.get(slug)
            if cat:
                return cat, False
            
            cat, created = Category.objects.get_or_create(
                slug=slug,
                defaults={'name': name}
            )
            
            if created:
                self.categories_by_slug[slug] = cat
                self.categories_by_id[cat.pk] = cat
                logger.info(f"Created new category: {name} ({slug})")
            
            return cat, created
    
    def match_tags_to_categories(self, tags: List[str]) -> List[Any]:
        """
        Match article tags to categories using keyword mapping.
        Returns list of matching categories sorted by relevance.
        """
        self.load()
        
        if not tags:
            return []
        
        # Score categories by number of matching tags
        category_scores: Dict[int, int] = defaultdict(int)
        category_objects: Dict[int, Any] = {}
        
        for tag in tags:
            tag_lower = tag.lower().strip()
            
            # Direct keyword match
            if tag_lower in self.keyword_to_categories:
                for cat in self.keyword_to_categories[tag_lower]:
                    category_scores[cat.id] += 2  # Higher weight for direct match
                    category_objects[cat.id] = cat
            
            # Partial match (tag contains keyword or keyword contains tag)
            for keyword, cats in self.keyword_to_categories.items():
                if tag_lower in keyword or keyword in tag_lower:
                    for cat in cats:
                        if cat.id not in category_scores:
                            category_scores[cat.id] += 1
                            category_objects[cat.id] = cat
        
        # Sort by score (descending) and return
        sorted_cats = sorted(
            category_objects.values(),
            key=lambda c: category_scores[c.id],
            reverse=True
        )
        
        return sorted_cats[:5]  # Return top 5 matches


def get_category_cache() -> CategoryCache:
    """Get the singleton category cache."""
    return CategoryCache()


# =============================================================================
# BULK ARTICLE OPERATIONS
# =============================================================================

def generate_hash(text: str) -> str:
    """Generate SHA256 hash for text."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest() if text else ""


class ArticleBulkProcessor:
    """
    Processes articles in bulk for efficient database operations.
    """
    
    def __init__(self, batch_size: int = 100):
        self.batch_size = batch_size
        self.category_cache = get_category_cache()
        
        # Statistics
        self.stats = {
            'created': 0,
            'updated': 0,
            'skipped': 0,
            'failed': 0
        }
    
    def process_articles(
        self,
        articles: List[Dict[str, Any]],
        ai_results: Optional[Dict[str, Dict]] = None,
        skip_existing: bool = True
    ) -> Dict[str, int]:
        """
        Process and save articles in bulk.
        
        Args:
            articles: List of article data dicts
            ai_results: Optional dict mapping URL to AI results {'summary', 'tags'}
            skip_existing: Skip articles that already exist (by URL)
        
        Returns:
            Statistics dict with counts
        """
        from web_app.models import NewsArticle, Category
        
        self.category_cache.load()
        ai_results = ai_results or {}
        
        # Reset stats
        self.stats = {'created': 0, 'updated': 0, 'skipped': 0, 'failed': 0}
        
        # Filter out invalid articles
        valid_articles = []
        for article in articles:
            url = article.get('url')
            if not url:
                self.stats['skipped'] += 1
                continue
            
            if article.get('error') or article.get('skipped'):
                self.stats['skipped'] += 1
                continue
            
            if not article.get('title') and not article.get('content'):
                self.stats['skipped'] += 1
                continue
            
            valid_articles.append(article)
        
        if not valid_articles:
            logger.warning("No valid articles to process")
            return self.stats
        
        # Get existing URLs in one query
        urls = [a.get('url') for a in valid_articles]
        existing_urls = set(
            NewsArticle.objects.filter(url__in=urls).values_list('url', flat=True)
        )
        
        # Separate new and existing articles
        new_articles = []
        for article in valid_articles:
            url = article.get('url')
            if url in existing_urls:
                if skip_existing:
                    self.stats['skipped'] += 1
                else:
                    # Would update - but for now we skip
                    self.stats['skipped'] += 1
            else:
                new_articles.append(article)
        
        if not new_articles:
            logger.info("All articles already exist in database")
            return self.stats
        
        # Process in batches using bulk_create
        logger.info(f"Creating {len(new_articles)} new articles in batches of {self.batch_size}")
        
        for i in range(0, len(new_articles), self.batch_size):
            batch = new_articles[i:i + self.batch_size]
            self._process_batch(batch, ai_results)
        
        logger.info(f"Bulk processing complete: {self.stats}")
        return self.stats
    
    def _process_batch(
        self,
        articles: List[Dict[str, Any]],
        ai_results: Dict[str, Dict]
    ):
        """Process a batch of articles."""
        from web_app.models import NewsArticle
        
        article_objects = []
        article_categories = []  # List of (article_index, category) tuples
        
        for idx, article in enumerate(articles):
            try:
                url = article.get('url')
                if not url:
                    self.stats['skipped'] += 1
                    continue
                    
                s_hash = generate_hash(str(url))
                
                # Get AI results if available
                ai_data = ai_results.get(str(url), {}) if ai_results else {}
                summary = ai_data.get('summary', '')
                tags = ai_data.get('tags', [])
                
                # Get fallback category from RSS category
                cat_slug = article.get('cat', 'uncategorized').lower().replace(' ', '-')
                fallback_category, _ = self.category_cache.get_or_create_by_slug(
                    cat_slug,
                    article.get('cat', 'Uncategorized').title()
                )
                
                # Match tags to categories
                matched_categories = []
                if tags:
                    matched_categories = self.category_cache.match_tags_to_categories(tags)
                
                primary_category = matched_categories[0] if matched_categories else fallback_category
                
                # Create article object (not saved yet)
                article_obj = NewsArticle(
                    title=article.get('title', 'No Title'),
                    url=url,
                    source_hash=s_hash,
                    source_name=url.split('//')[-1].split('/')[0] if url else "Unknown",
                    raw_content=article.get('content'),
                    ai_summary=summary,
                    tags=tags,
                    primary_category=primary_category,
                    published_at=article.get('published_date'),
                )
                
                article_objects.append(article_obj)
                
                # Store categories for M2M relationship
                categories_to_set = matched_categories if matched_categories else [fallback_category]
                article_categories.append((idx, categories_to_set))
                
            except Exception as e:
                logger.error(f"Error preparing article {article.get('url')}: {e}")
                self.stats['failed'] += 1
        
        # Bulk create articles
        if article_objects:
            try:
                with transaction.atomic():
                    created = NewsArticle.objects.bulk_create(
                        article_objects,
                        ignore_conflicts=True,
                        update_conflicts=False
                    )
                    
                    # Set M2M relationships (requires saved objects)
                    # Re-fetch the created articles to set categories
                    created_urls = [a.url for a in article_objects]
                    saved_articles = {
                        a.url: a 
                        for a in NewsArticle.objects.filter(url__in=created_urls)
                    }
                    
                    for idx, categories in article_categories:
                        if idx < len(article_objects):
                            url = article_objects[idx].url
                            if url in saved_articles:
                                saved_articles[url].categories.set(categories)
                    
                    self.stats['created'] += len(created)
                    logger.debug(f"Batch created {len(created)} articles")
                    
            except Exception as e:
                logger.error(f"Bulk create failed: {e}")
                self.stats['failed'] += len(article_objects)


def create_articles_bulk(
    articles: List[Dict[str, Any]],
    ai_results: Optional[Dict[str, Dict]] = None
) -> Dict[str, int]:
    """
    Convenience function to bulk create articles.
    
    Args:
        articles: List of article data dicts with 'url', 'title', 'content', 'cat', 'published_date'
        ai_results: Optional dict mapping URL to {'summary': str, 'tags': list}
    
    Returns:
        Statistics dict
    """
    processor = ArticleBulkProcessor()
    return processor.process_articles(articles, ai_results)


# =============================================================================
# OPTIMIZED CATEGORY MATCHER
# =============================================================================

def assign_categories_to_article_fast(
    article,
    tags: List[str],
    fallback_category=None
):
    """
    Fast category assignment using cached keywords.
    Drop-in replacement for assign_categories_to_article.
    """
    cache = get_category_cache()
    cache.load()
    
    matched_categories = cache.match_tags_to_categories(tags) if tags else []
    
    if matched_categories:
        primary = matched_categories[0]
        article.primary_category = primary
        article.tags = tags
        article.save()
        article.categories.set(matched_categories)
    elif fallback_category:
        article.primary_category = fallback_category
        article.tags = tags
        article.save()
        article.categories.set([fallback_category])
    else:
        article.tags = tags
        article.save()
    
    return matched_categories or ([fallback_category] if fallback_category else [])
