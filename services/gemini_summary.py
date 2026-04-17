from google import genai
from decouple import config
import time
import json
import logging

logger = logging.getLogger(__name__)

# Configure the Gemini API
client = genai.Client(api_key=config('GEMINI_API_KEY'),)


def summarize_with_gemini(text: str) -> str:
    """
    Summarizes a single news post into 3 punchy bullet points using Gemini.
    
    Args:
        text: Article content to summarize
        
    Returns:
        Summary string
    """
    if not text or len(text) < 50:
        return "Content too short to summarize."

    prompt = (
        "You are a professional news editor. Summarize the following news article "
        "into exactly 3 concise bullet points. Focus on the 'who, what, and why'. "
        "Keep it under 100 words total.\n\n"
        f"Article Content: {text}"
    )
    
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash", contents=prompt
        )
        time.sleep(2)
        # Handle cases where the model might return empty or blocked content
        if response.text:
            return response.text.strip()
        return "Summary generation resulted in empty content."
    except Exception as e:
        return f"Gemini summary failed: {str(e)}"


def summarize_and_tag_with_gemini(text: str, title: str = "") -> dict:
    """
    Generate both summary and tags for an article in a single API call.
    
    Args:
        text: Article content
        title: Article title (optional, helps with tagging)
        
    Returns:
        Dict with 'summary' and 'tags' keys
        Example: {
            'summary': '• Point 1\n• Point 2\n• Point 3',
            'tags': ['technology', 'ai', 'google', 'gemini']
        }
    """
    if not text or len(text) < 50:
        return {
            'summary': "Content too short to summarize.",
            'tags': []
        }
    
    context = f"Title: {title}\n\n" if title else ""
    
    prompt = f"""You are a professional news editor and content tagger.

Analyze the following news article and provide:
1. A summary in exactly 3 concise bullet points (under 100 words total)
2. 5-10 relevant tags for categorization (lowercase, specific topics/entities/themes)

{context}Article Content:
{text[:4000]}

Respond in this exact JSON format only (no markdown, no code blocks):
{{
    "summary": "• Point 1\\n• Point 2\\n• Point 3",
    "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}}

Rules for tags:
- Use lowercase only
- Include specific entities (company names, people, technologies)
- Include broad themes (politics, technology, sports, business, etc.)
- Include industry-specific terms
- No hashtags, just plain words/phrases"""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash", 
            contents=prompt,
            config={
                "response_mime_type": "application/json"
            }
        )
        
        time.sleep(2)
        
        if response.text:
            # Clean up response (remove markdown code blocks if present)
            content = response.text.strip()
            if content.startswith('```'):
                content = content.split('```')[1]
                if content.startswith('json'):
                    content = content[4:]
            content = content.strip()
            
            result = json.loads(content)
            # Normalize tags to lowercase
            tags = [tag.lower().strip() for tag in result.get('tags', []) if tag]
            return {
                'summary': result.get('summary', '').strip(),
                'tags': tags
            }
        
        return {'summary': "Summary generation resulted in empty content.", 'tags': []}
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error in Gemini response: {e}")
        # Fallback to summary-only
        return {
            'summary': summarize_with_gemini(text),
            'tags': []
        }
    except Exception as e:
        logger.error(f"Gemini summarize_and_tag failed: {e}")
        return {
            'summary': f"Gemini summary failed: {str(e)}",
            'tags': []
        }