"""
Celery tasks for scraping and summary generation.
"""

from celery import shared_task
from django.core.management import call_command
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def run_daily_scraper(self):
    """
    Celery task that runs the scraper management command.
    Executes daily to fetch articles from the last 24 hours.
    Uses the optimized fast scraper by default.
    """
    try:
        logger.info(f"Starting daily scraper task at {timezone.now()}")
        
        # Use the fast scraper for better performance
        call_command('run_scraper_fast', 
                     hours=24, 
                     workers=10, 
                     ai_workers=20,
                     log_file='logs/daily_scraper_{date}.log')
        
        logger.info(f"Daily scraper task completed at {timezone.now()}")
        return {
            'status': 'success',
            'completed_at': str(timezone.now())
        }
    
    except Exception as exc:
        logger.error(f"Scraper task failed: {exc}")
        # Retry the task on failure
        raise self.retry(exc=exc)


@shared_task
def run_scraper_manual(hours=24, fast=True, workers=10, ai_workers=20):
    """
    Manual trigger for the scraper with custom settings.
    Can be called from Django admin or API.
    
    Args:
        hours: How many hours back to scrape (default: 24)
        fast: Use optimized concurrent scraper (default: True)
        workers: Number of concurrent workers for scraping (default: 10)
        ai_workers: Number of concurrent workers for AI (default: 20)
    """
    try:
        logger.info(f"Starting manual scraper task for last {hours} hours (fast={fast})")
        
        if fast:
            call_command('run_scraper_fast', 
                         hours=hours, 
                         workers=workers, 
                         ai_workers=ai_workers)
        else:
            call_command('run_scraper', f'--hours={hours}')
        
        return {'status': 'success', 'hours': hours, 'fast': fast}
    except Exception as exc:
        logger.error(f"Manual scraper task failed: {exc}")
        return {'status': 'failed', 'error': str(exc)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def scrape_single_rss_source(self, source_id):
    """
    Scrape articles from a single RSS feed source.
    
    Args:
        source_id: ID of the RSSFeedSource to scrape
    """
    from web_app.models import RSSFeedSource, NewsArticle, Category
    from scraper import rss_links, post_links
    from services.openai_summary import summarize_and_tag_with_openai
    from services.category_matcher import assign_categories_to_article
    import hashlib
    
    try:
        source = RSSFeedSource.objects.get(id=source_id)
        logger.info(f"Starting scrape for RSS source: {source.name} ({source.feed_url})")
        
        # Extract articles from RSS feed (use category slug for consistency)
        urls_with_cat = [(source.category.slug, source.feed_url)]
        rss_data = rss_links.extract(urls_with_cat, max_age_hours=24)
        
        articles_created = 0
        
        for item in rss_data:
            try:
                # rss_data items are tuples: (category, title, link, pub_date)
                cat, title, link, pub_date = item
                
                # Skip if URL already exists
                if NewsArticle.objects.filter(url=link).exists():
                    continue
                
                # Extract article content
                article_data = post_links.extract(
                    link,
                    rss_pub_date=pub_date
                )
                
                if not article_data or not article_data.get('title'):
                    continue
                
                # Generate summary AND tags in one call
                result = {'summary': None, 'tags': []}
                if article_data.get('content'):
                    result = summarize_and_tag_with_openai(
                        article_data['content'],
                        title=article_data.get('title', '')
                    )
                
                # Generate hash for deduplication
                content_hash = hashlib.sha256(
                    (article_data.get('url', '') + article_data.get('title', '')).encode()
                ).hexdigest()
                
                # Create article (without category initially)
                article = NewsArticle.objects.create(
                    rss_source=source,
                    title=article_data.get('title', '')[:500],
                    source_name=source.name,
                    url=article_data.get('url'),
                    raw_content=article_data.get('content'),
                    ai_summary=result['summary'],
                    source_hash=content_hash,
                    published_at=article_data.get('published_date')
                )
                
                # Assign categories based on tags (fallback to source's category)
                assign_categories_to_article(
                    article,
                    tags=result['tags'],
                    fallback_category=source.category
                )
                
                articles_created += 1
                
            except Exception as e:
                logger.warning(f"Error processing article: {e}")
                continue
        
        # Update source statistics
        source.last_scraped_at = timezone.now()
        source.articles_count += articles_created
        source.error_message = None
        source.save()
        
        logger.info(f"Completed scraping {source.name}: {articles_created} articles created")
        
        return {
            'status': 'success',
            'source_id': source_id,
            'articles_created': articles_created
        }
        
    except RSSFeedSource.DoesNotExist:
        logger.error(f"RSS source {source_id} not found")
        return {'status': 'failed', 'error': 'Source not found'}
        
    except Exception as exc:
        logger.error(f"Scrape failed for source {source_id}: {exc}")
        
        # Update source with error
        try:
            source = RSSFeedSource.objects.get(id=source_id)
            source.error_message = str(exc)
            source.save()
        except Exception:
            pass
        
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def scrape_category_sources(self, category_id):
    """
    Scrape all active RSS sources for a category.
    
    Args:
        category_id: ID of the Category to scrape sources for
    """
    from web_app.models import Category, RSSFeedSource
    
    try:
        category = Category.objects.get(id=category_id)
        sources = RSSFeedSource.objects.filter(
            category=category,
            status=RSSFeedSource.Status.ACTIVE
        )
        
        logger.info(f"Starting scrape for category '{category.name}' ({sources.count()} sources)")
        
        results = []
        for source in sources:
            # Chain tasks for each source
            task = scrape_single_rss_source.delay(source.pk) # type: ignore[attr-defined]
            results.append({
                'source_id': source.pk,
                'source_name': source.name,
                'task_id': task.id
            })
        
        return {
            'status': 'success',
            'category_id': category_id,
            'category_name': category.name,
            'sources_queued': len(results),
            'tasks': results
        }
        
    except Category.DoesNotExist:
        logger.error(f"Category {category_id} not found")
        return {'status': 'failed', 'error': 'Category not found'}
        
    except Exception as exc:
        logger.error(f"Category scrape failed: {exc}")
        raise self.retry(exc=exc)


@shared_task
def generate_daily_summaries():
    """
    Generate daily summaries for all active categories.
    Should be scheduled to run after the daily scraper.
    """
    from services.summary_service import regenerate_all_summaries_for_date
    from datetime import date
    
    try:
        logger.info("Starting daily summary generation")
        regenerate_all_summaries_for_date(date.today())
        
        return {
            'status': 'success',
            'date': str(date.today()),
            'completed_at': str(timezone.now())
        }
        
    except Exception as exc:
        logger.error(f"Daily summary generation failed: {exc}")
        return {'status': 'failed', 'error': str(exc)}


@shared_task(bind=True, max_retries=2)
def validate_rss_source(self, source_id):
    """
    Validate an RSS feed source and update its status.
    
    Args:
        source_id: ID of the RSSFeedSource to validate
    """
    from web_app.models import RSSFeedSource
    from services.rss_discovery import validate_rss_feed
    
    try:
        source = RSSFeedSource.objects.get(id=source_id)
        
        is_valid, info = validate_rss_feed(source.feed_url)
        
        if is_valid:
            source.status = RSSFeedSource.Status.ACTIVE
            source.error_message = None
        else:
            source.status = RSSFeedSource.Status.ERROR
            source.error_message = info.get('error', 'Unknown validation error')
        
        source.save()
        
        return {
            'status': 'success',
            'source_id': source_id,
            'is_valid': is_valid,
            'info': info
        }
        
    except RSSFeedSource.DoesNotExist:
        return {'status': 'failed', 'error': 'Source not found'}
        
    except Exception as exc:
        logger.error(f"Validation failed for source {source_id}: {exc}")
        raise self.retry(exc=exc)
