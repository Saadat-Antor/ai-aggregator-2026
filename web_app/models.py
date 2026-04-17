from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Category(models.Model):
    """
    Represents a news category (e.g., Sports, Technology, Business).
    """
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    
    # Keywords used to match article tags to this category
    # Example: ["nba", "nfl", "soccer", "football", "basketball", "sports"]
    keywords = models.JSONField(
        default=list,
        blank=True,
        help_text="List of keywords/tags that map articles to this category"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ['name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class RSSFeedSource(models.Model):
    """
    Stores RSS feed URLs associated with categories.
    Supports user-added feeds for dynamic content sourcing.
    """
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        INACTIVE = 'inactive', 'Inactive'
        ERROR = 'error', 'Error'
        PENDING = 'pending', 'Pending Validation'

    category = models.ForeignKey(
        Category, 
        on_delete=models.CASCADE, 
        related_name='rss_sources'
    )
    name = models.CharField(max_length=255, help_text="Source name (e.g., ESPN, BBC)")
    website_url = models.URLField(max_length=500, help_text="Main website URL")
    feed_url = models.URLField(max_length=1000, unique=True, help_text="RSS feed URL")
    status = models.CharField(
        max_length=20, 
        choices=Status.choices, 
        default=Status.PENDING
    )
    last_scraped_at = models.DateTimeField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    articles_count = models.PositiveIntegerField(default=0)
    is_user_submitted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "RSS Feed Source"
        verbose_name_plural = "RSS Feed Sources"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.category.name})"


class NewsArticle(models.Model):
    """
    Stores individual news articles with their content and AI summaries.
    """
    # Primary category (highest confidence match or source default)
    primary_category = models.ForeignKey(
        Category, 
        on_delete=models.CASCADE, 
        related_name='primary_articles',
        null=True,
        blank=True
    )
    
    # All categories this article belongs to (computed from tags)
    categories = models.ManyToManyField(
        Category,
        related_name='articles',
        blank=True
    )
    
    rss_source = models.ForeignKey(
        RSSFeedSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='articles'
    )
    title = models.CharField(max_length=500)
    source_name = models.CharField(max_length=256)
    url = models.URLField(max_length=1000, unique=True, blank=True, null=True)
    
    # The full scraped text content
    raw_content = models.TextField(blank=True, null=True)
    
    # The AI-generated summary for this specific article
    ai_summary = models.TextField(blank=True, null=True)
    
    # AI-generated tags for categorization
    # Example: ["tesla", "electric vehicles", "stock market", "elon musk"]
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text="AI-generated tags for category matching"
    )
    
    # For Deduplication: Store a hash of the content or the URL
    source_hash = models.CharField(
        max_length=64, 
        unique=True, 
        db_index=True, 
        blank=True, 
        null=True
    )
    
    published_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-published_at']
        verbose_name = "News Article"
        verbose_name_plural = "News Articles"
        indexes = [
            models.Index(fields=['primary_category', 'published_at']),
            models.Index(fields=['created_at']),
            models.Index(fields=['published_at']),
        ]

    def __str__(self):
        return self.title


class DailyCategorySummary(models.Model):
    """
    Stores the pre-calculated 'Mega-Summary' for a category.
    This is what the API serves to users for an instant overview.
    """
    category = models.ForeignKey(
        Category, 
        on_delete=models.CASCADE,
        related_name='daily_summaries'
    )
    combined_summary = models.TextField(blank=True, null=True)
    date = models.DateField(default=timezone.now, db_index=True)
    
    # Tracks which articles were used to create this summary
    article_count = models.PositiveIntegerField(default=0)
    articles_used = models.ManyToManyField(
        NewsArticle, 
        blank=True,
        related_name='used_in_summaries'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
        models.UniqueConstraint(fields=['category', 'date'], name='unique_category_date')
            ]
        verbose_name = "Daily Category Summary"
        verbose_name_plural = "Daily Category Summaries"
        ordering = ['-date']

    def __str__(self):
        return f"{self.category.name} Summary - {self.date}"
