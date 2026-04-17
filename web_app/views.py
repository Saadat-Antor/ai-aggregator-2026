"""
API Views for the AI News Aggregator.
Provides endpoints for category summaries, article management, and RSS feed operations.
"""

from rest_framework import viewsets, status, generics
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.utils import timezone
from django.db.models import Q, Count
from django.shortcuts import get_object_or_404
from datetime import date, timedelta
import logging

from .models import Category, RSSFeedSource, NewsArticle, DailyCategorySummary
from .serializers import (
    CategorySerializer,
    CategoryCreateSerializer,
    RSSFeedSourceSerializer,
    RSSFeedSourceCreateSerializer,
    NewsArticleSerializer,
    NewsArticleDetailSerializer,
    DailyCategorySummarySerializer,
    CategorySummaryRequestSerializer,
    MultiCategorySummaryRequestSerializer,
    SummaryResponseSerializer,
    AddCategoryWithSourceRequestSerializer,
    TaskStatusSerializer,
    UserInfoSerializer,
)

# User info endpoint
from rest_framework.permissions import IsAuthenticated
class UserInfoView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        serializer = UserInfoSerializer(request.user)
        return Response(serializer.data)

logger = logging.getLogger(__name__)


class StandardResultsPagination(PageNumberPagination):
    """Standard pagination for list endpoints."""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


# =============================================================================
# Category ViewSet
# =============================================================================

class CategoryViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing news categories.
    
    list: Get all active categories
    retrieve: Get a single category by ID or slug
    create: Create a new category
    update: Update an existing category
    delete: Soft-delete a category (sets is_active=False)
    """
    queryset = Category.objects.filter(is_active=True)
    permission_classes = [AllowAny]
    pagination_class = StandardResultsPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']
    lookup_field = 'slug'

    def get_serializer_class(self): # type: ignore[override]
        if self.action == 'create':
            return CategoryCreateSerializer
        return CategorySerializer

    def get_object(self):
        """Allow lookup by both slug and pk."""
        lookup_value = self.kwargs.get(self.lookup_field)
        
        # Try to get by slug first
        queryset = self.get_queryset()
        obj = queryset.filter(slug=lookup_value).first()
        
        # If not found by slug, try by pk
        if obj is None and lookup_value.isdigit():
            obj = queryset.filter(pk=lookup_value).first()
        
        if obj is None:
            from django.http import Http404
            raise Http404("Category not found")
        
        self.check_object_permissions(self.request, obj)
        return obj

    def destroy(self, request, *args, **kwargs):
        """Soft delete - set is_active to False instead of deleting."""
        instance = self.get_object()
        instance.is_active = False
        instance.save()
        return Response(
            {"message": f"Category '{instance.name}' has been deactivated."},
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['get'])
    def articles(self, request, slug=None):
        """Get all articles for a specific category."""
        category = self.get_object()
        articles = NewsArticle.objects.filter(categories=category)
        
        # Apply pagination
        paginator = StandardResultsPagination()
        page = paginator.paginate_queryset(articles, request)
        serializer = NewsArticleSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @action(detail=True, methods=['get'])
    def summary(self, request, slug=None):
        """Get the daily summary for a specific category."""
        category = self.get_object()
        target_date = request.query_params.get('date', date.today())
        
        if isinstance(target_date, str):
            from datetime import datetime
            try:
                target_date = datetime.strptime(target_date, '%Y-%m-%d').date()
            except ValueError:
                target_date = date.today()
        
        summary = DailyCategorySummary.objects.filter(
            category=category,
            date=target_date
        ).first()
        
        if not summary:
            return Response(
                {"message": "No summary available for this date.", "category": category.name},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = DailyCategorySummarySerializer(summary)
        return Response({"data":serializer.data},
                         status=status.HTTP_200_OK)


# =============================================================================
# RSS Feed Source ViewSet
# =============================================================================

class RSSFeedSourceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing RSS feed sources.
    """
    queryset = RSSFeedSource.objects.all()
    permission_classes = [AllowAny]
    pagination_class = StandardResultsPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['category', 'status', 'is_user_submitted']
    search_fields = ['name', 'website_url', 'feed_url']
    ordering_fields = ['name', 'created_at', 'articles_count']
    ordering = ['-created_at']

    def get_serializer_class(self): # type: ignore[override]
        if self.action == 'create':
            return RSSFeedSourceCreateSerializer
        return RSSFeedSourceSerializer

    @action(detail=True, methods=['post'])
    def validate_feed(self, request, pk=None):
        """Validate and activate an RSS feed source."""
        source = self.get_object()
        
        # Import the validation service
        from services.rss_discovery import validate_rss_feed
        
        try:
            is_valid, message = validate_rss_feed(source.feed_url)
            
            if is_valid:
                source.status = RSSFeedSource.Status.ACTIVE
                source.error_message = None
            else:
                source.status = RSSFeedSource.Status.ERROR
                source.error_message = message
            
            source.save()
            
            return Response({
                "valid": is_valid,
                "message": message,
                "status": source.status
            })
        except Exception as e:
            logger.error(f"Feed validation error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def scrape_now(self, request, pk=None):
        """Trigger immediate scraping for this RSS source."""
        source = self.get_object()
        
        if source.status != RSSFeedSource.Status.ACTIVE:
            return Response(
                {"error": "Cannot scrape inactive or errored feed sources."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Trigger async scraping task
        from tasks.scraper_tasks import scrape_single_rss_source
        task = scrape_single_rss_source.delay(source.pk) # type: ignore[attr-defined]
        
        return Response({
            "message": f"Scraping task started for {source.name}",
            "task_id": task.id,
            "source_id": source.pk
        }, status=status.HTTP_202_ACCEPTED)


# =============================================================================
# News Article ViewSet
# =============================================================================

class NewsArticleViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for reading news articles (read-only).
    Articles are created by the scraper, not directly via API.
    """
    queryset = NewsArticle.objects.select_related(
        'primary_category', 'rss_source'
    ).prefetch_related('categories')
    permission_classes = [AllowAny]
    pagination_class = StandardResultsPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['primary_category', 'source_name']
    search_fields = ['title', 'ai_summary', 'source_name']
    ordering_fields = ['published_at', 'created_at', 'title']
    ordering = ['-published_at']

    def get_serializer_class(self): # type: ignore[override]
        if self.action == 'retrieve':
            return NewsArticleDetailSerializer
        return NewsArticleSerializer


# =============================================================================
# Summary API Views
# =============================================================================

class SingleCategorySummaryView(APIView):
    """
    API endpoint to get a collective summary for a single category.
    
    POST /api/v1/summaries/category/
    
    Request Body:
    {
        "category_id": 1,        // OR
        "category_slug": "tech",
        "date": "2026-02-15",    // Optional, defaults to today
        "regenerate": false      // Optional, force regeneration
    }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CategorySummaryRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        data: dict = serializer.validated_data # type: ignore[assignment]
        target_date = data.get('date', date.today())
        regenerate = data.get('regenerate', False) 
        
        # Find the category
        category = None
        if data.get('category_id'): 
            category = get_object_or_404(Category, id=data['category_id'], is_active=True)
        elif data.get('category_slug'):
            category = get_object_or_404(Category, slug=data['category_slug'], is_active=True)
        
        # Get or generate summary
        summary = self._get_or_generate_summary(category, target_date, regenerate)
        
        if category:
            return Response({
                "category": {
                    "id": category.pk,
                    "name": category.name,
                    "slug": category.slug
                },
                "summary": summary.combined_summary if summary else None,
                "article_count": summary.article_count if summary else 0,
                "date": target_date,
                "last_updated": summary.last_updated if summary else None,
                "generated_at": timezone.now()
            })

    def _get_or_generate_summary(self, category, target_date, regenerate=False):
        """Get existing summary or generate a new one."""
        summary = DailyCategorySummary.objects.filter(
            category=category,
            date=target_date
        ).first()
        
        if summary and not regenerate:
            return summary
        
        # Generate new summary from articles
        from services.summary_service import generate_category_summary
        return generate_category_summary(category, target_date)


class MultiCategorySummaryView(APIView):
    """
    API endpoint to get a collective summary for multiple categories.
    
    POST /api/v1/summaries/categories/
    
    Request Body:
    {
        "category_ids": [1, 2, 3],       // OR
        "category_slugs": ["tech", "business"],
        "date": "2026-02-15",            // Optional
        "regenerate": false              // Optional
    }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = MultiCategorySummaryRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        data: dict = serializer.validated_data # type: ignore[assignment]
        target_date = data.get('date', date.today())
        regenerate = data.get('regenerate', False)
        
        # Collect categories
        categories = []
        
        if data.get('category_ids'):
            categories.extend(
                Category.objects.filter(
                    id__in=data['category_ids'],
                    is_active=True
                )
            )
        
        if data.get('category_slugs'):
            categories.extend(
                Category.objects.filter(
                    slug__in=data['category_slugs'],
                    is_active=True
                )
            )
        
        # Remove duplicates while preserving order
        seen = set()
        unique_categories = []
        for cat in categories:
            if cat.pk not in seen:
                seen.add(cat.pk)
                unique_categories.append(cat)
        
        if not unique_categories:
            return Response(
                {"error": "No valid categories found."},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get summaries for each category
        category_summaries = []
        total_articles = 0
        all_summaries_text = []
        
        for category in unique_categories:
            summary = self._get_or_generate_summary(category, target_date, regenerate)
            
            category_data = {
                "id": category.pk,
                "name": category.name,
                "slug": category.slug,
                "summary": summary.combined_summary if summary else None,
                "article_count": summary.article_count if summary else 0
            }
            category_summaries.append(category_data)
            
            if summary:
                total_articles += summary.article_count
                if summary.combined_summary:
                    all_summaries_text.append(f"**{category.name}:**\n{summary.combined_summary}")
        
        # Generate combined summary if multiple categories
        combined_summary = None
        if len(unique_categories) > 1 and all_summaries_text:
            from services.summary_service import generate_multi_category_summary
            combined_summary = generate_multi_category_summary(all_summaries_text)
        elif len(unique_categories) == 1 and category_summaries[0]['summary']:
            combined_summary = category_summaries[0]['summary']
        
        return Response({
            "categories": category_summaries,
            "combined_summary": combined_summary,
            "total_articles": total_articles,
            "date": target_date,
            "generated_at": timezone.now()
        })

    def _get_or_generate_summary(self, category, target_date, regenerate=False):
        """Get existing summary or generate a new one."""
        summary = DailyCategorySummary.objects.filter(
            category=category,
            date=target_date
        ).first()
        
        if summary and not regenerate:
            return summary
        
        from services.summary_service import generate_category_summary
        return generate_category_summary(category, target_date)


# =============================================================================
# Add New Category with RSS Source
# =============================================================================

class AddCategoryWithSourceView(APIView):
    """
    API endpoint to add a new category with a news website.
    Discovers RSS feeds and starts scraping automatically.
    
    POST /api/v1/categories/add-with-source/
    
    Request Body:
    {
        "category_name": "AI News",
        "category_description": "Latest artificial intelligence news",
        "website_url": "https://techcrunch.com",
        "source_name": "TechCrunch",  // Optional
        "auto_scrape": true           // Optional, default true
    }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = AddCategoryWithSourceRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        data: dict = serializer.validated_data # type: ignore[assignment]
        
        try:
            # Step 1: Discover RSS feeds from the website
            from services.rss_discovery import discover_rss_feeds
            
            logger.info(f"Discovering RSS feeds from {data['website_url']}")
            discovered_feeds = discover_rss_feeds(data['website_url'])
            
            if not discovered_feeds:
                return Response({
                    "error": "No RSS feeds found on the provided website.",
                    "suggestion": "Try providing a direct RSS feed URL instead.",
                    "website_url": data['website_url']
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Step 2: Create the category
            category, created = Category.objects.get_or_create(
                name=data['category_name'],
                defaults={
                    "description": data.get('category_description', ''),
                    "is_active": True
                }
            )
            
            # Step 3: Create RSS feed sources
            source_name = data.get('source_name') or self._extract_source_name(data['website_url'])
            created_sources = []
            
            for feed_info in discovered_feeds[:5]:  # Limit to 5 feeds per source
                try:
                    source = RSSFeedSource.objects.create(
                        category=category,
                        name=source_name,
                        website_url=data['website_url'],
                        feed_url=feed_info['url'],
                        status=RSSFeedSource.Status.ACTIVE,
                        is_user_submitted=True
                    )
                    created_sources.append({
                        "id": source.pk,
                        "feed_url": source.feed_url,
                        "title": feed_info.get('title', 'Unknown')
                    })
                except Exception as e:
                    logger.warning(f"Could not create source: {e}")
                    continue
            
            if not created_sources:
                # Rollback category creation if no sources were added
                if created:
                    category.delete()
                return Response({
                    "error": "Failed to create RSS sources. Feeds may already exist.",
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Step 4: Trigger scraping if auto_scrape is enabled
            task_id = None
            if data.get('auto_scrape', True):
                from tasks.scraper_tasks import scrape_category_sources
                task = scrape_category_sources.delay(category.pk) # type: ignore[attr-defined]
                task_id = task.id
            
            return Response({
                "message": f"Category '{category.name}' created successfully.",
                "category": {
                    "id": category.pk,
                    "name": category.name,
                    "slug": category.slug
                },
                "rss_sources": created_sources,
                "scraping_task_id": task_id,
                "feeds_discovered": len(discovered_feeds),
                "feeds_added": len(created_sources)
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error adding category with source: {e}")
            return Response({
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _extract_source_name(self, url):
        """Extract a readable source name from URL."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.replace('www.', '')
        return domain.split('.')[0].title()


# =============================================================================
# Task Status View
# =============================================================================

class TaskStatusView(APIView):
    """
    Check the status of an async task.
    
    GET /api/v1/tasks/{task_id}/
    """
    permission_classes = [AllowAny]

    def get(self, request, task_id):
        from celery.result import AsyncResult
        
        task = AsyncResult(task_id)
        
        response_data = {
            "task_id": task_id,
            "status": task.status.lower(),
            "created_at": timezone.now()  # Celery doesn't store creation time by default
        }
        
        if task.successful():
            response_data["result"] = task.result
        elif task.failed():
            response_data["error"] = str(task.result)
        
        return Response(response_data)


# =============================================================================
# Health Check View
# =============================================================================

class HealthCheckView(APIView):
    """
    API health check endpoint.
    
    GET /api/v1/health/
    """
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            "status": "healthy",
            "timestamp": timezone.now(),
            "version": "1.0.0"
        })
