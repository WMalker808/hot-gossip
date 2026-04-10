#!/usr/bin/env python3
"""
Guardian Comments Scraper
Extracts all comments from a Guardian article and saves to JSON.
Supports keyword-based article search via Guardian Content API.
"""

import requests
import json
import re
import sys
import os
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse


def extract_short_url(article_url: str) -> str:
    """Extract the discussion short URL key from a Guardian article page."""
    response = requests.get(article_url, timeout=30)
    response.raise_for_status()

    # Look for the shortUrl in the page's JSON-LD or data attributes
    # Pattern: "shortUrl":"https://www.theguardian.com/p/xxxxx"
    match = re.search(r'"shortUrl"\s*:\s*"https?://(?:www\.)?theguardian\.com(/p/[a-z0-9]+)"', response.text)
    if match:
        return match.group(1)

    # Alternative pattern: data-short-url or shortUrlId
    match = re.search(r'data-short-url="(/p/[a-z0-9]+)"', response.text)
    if match:
        return match.group(1)

    # Try finding discussion ID directly
    match = re.search(r'"discussionId"\s*:\s*"(/p/[a-z0-9]+)"', response.text)
    if match:
        return match.group(1)

    raise ValueError(f"Could not find discussion key in article: {article_url}")


def fetch_all_comments(short_url: str) -> dict:
    """Fetch all comments for a discussion, handling pagination."""
    base_url = "https://discussion.theguardian.com/discussion-api/discussion"

    all_comments = []
    page = 1
    page_size = 100  # Max allowed by API
    total_pages = 1
    discussion_info = {}

    while page <= total_pages:
        url = f"{base_url}{short_url}"
        params = {
            "page": page,
            "pageSize": page_size,
            "orderBy": "oldest",
            "displayThreaded": "false",  # Flat list, easier to process all
        }

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if page == 1:
            # Store discussion metadata from first request
            discussion_info = {
                "discussionId": data.get("discussion", {}).get("key"),
                "title": data.get("discussion", {}).get("title"),
                "webUrl": data.get("discussion", {}).get("webUrl"),
                "commentCount": data.get("discussion", {}).get("commentCount"),
                "isClosedForComments": data.get("discussion", {}).get("isClosedForComments"),
                "isClosedForRecommendation": data.get("discussion", {}).get("isClosedForRecommendation"),
            }
            total_pages = data.get("pages", 1)
            print(f"Found {discussion_info['commentCount']} comments across {total_pages} pages")

        comments = data.get("discussion", {}).get("comments", [])
        all_comments.extend(comments)

        print(f"Fetched page {page}/{total_pages} ({len(comments)} comments)")
        page += 1

    return {
        "discussion": discussion_info,
        "comments": all_comments,
        "totalFetched": len(all_comments),
        "scrapedAt": datetime.utcnow().isoformat() + "Z",
    }


def extract_articles_from_section(section_url: str, limit: int = 10) -> list:
    """
    Extract article URLs from a Guardian section/front page.

    Args:
        section_url: URL of a Guardian section (e.g., /lifeandstyle/health-and-wellbeing)
        limit: Maximum number of articles to return

    Returns:
        List of article dicts with title, url, and short_url (discussion key)
    """
    response = requests.get(section_url, timeout=30)
    response.raise_for_status()
    html = response.text

    # Find article links - Guardian uses data-link-name="article" or similar patterns
    # Look for links to article pages (not section links, not external)
    article_pattern = r'<a[^>]+href="(https://www\.theguardian\.com/[^"]+/\d{4}/[a-z]{3}/\d{2}/[^"]+)"[^>]*>([^<]*)</a>'
    matches = re.findall(article_pattern, html)

    # Also try pattern without full URL (relative links)
    relative_pattern = r'<a[^>]+href="(/[^"]+/\d{4}/[a-z]{3}/\d{2}/[^"]+)"[^>]*>'
    relative_matches = re.findall(relative_pattern, html)

    # Dedupe and collect unique article URLs
    seen_urls = set()
    articles = []

    for url, title in matches:
        if url not in seen_urls and len(articles) < limit:
            seen_urls.add(url)
            # We'll need to fetch the short_url from each article page
            articles.append({"url": url, "title": title.strip() if title.strip() else None})

    for path in relative_matches:
        url = f"https://www.theguardian.com{path}"
        if url not in seen_urls and len(articles) < limit:
            seen_urls.add(url)
            articles.append({"url": url, "title": None})

    # Now fetch the short_url (discussion key) for each article
    enriched_articles = []
    for article in articles[:limit]:
        try:
            short_url = extract_short_url(article["url"])
            # If we don't have a title, try to extract it
            if not article["title"]:
                resp = requests.get(article["url"], timeout=15)
                title_match = re.search(r'<title>([^<|]+)', resp.text)
                article["title"] = title_match.group(1).strip() if title_match else "Unknown"

            enriched_articles.append({
                "title": article["title"],
                "url": article["url"],
                "short_url": short_url,
                "section": "",
            })
        except (ValueError, requests.RequestException):
            # Skip articles without comments or that fail to load
            continue

    return enriched_articles


def search_articles_by_keyword(keyword: str, limit: int = 10) -> list:
    """
    Search Guardian Content API for articles matching a keyword.

    Args:
        keyword: Search term(s)
        limit: Maximum number of articles to return (1-20)

    Returns:
        List of article dicts with title, url, and short_url (discussion key)
    """
    api_key = os.environ.get("GUARDIAN_API_KEY")
    if not api_key:
        raise ValueError("GUARDIAN_API_KEY environment variable not set")

    limit = max(1, min(limit, 20))  # Clamp to 1-20

    url = "https://content.guardianapis.com/search"
    params = {
        "q": keyword,
        "page-size": limit,
        "show-fields": "shortUrl,headline",
        "order-by": "newest",
        "api-key": api_key,
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    results = data.get("response", {}).get("results", [])
    articles = []

    for item in results:
        fields = item.get("fields", {})
        short_url = fields.get("shortUrl", "")

        # Extract the /p/xxxxx part from shortUrl
        short_url_match = re.search(r"(/p/[a-z0-9]+)", short_url)
        if short_url_match:
            articles.append({
                "title": item.get("webTitle", "Unknown"),
                "url": item.get("webUrl", ""),
                "short_url": short_url_match.group(1),
                "section": item.get("sectionName", ""),
            })

    return articles


def fetch_comments_for_articles(articles: list, progress_callback=None) -> dict:
    """
    Fetch comments from multiple articles.

    Args:
        articles: List of article dicts from search_articles_by_keyword()
        progress_callback: Optional callback(article_index, article_title, comment_count)

    Returns:
        Dict with aggregated comments and metadata
    """
    all_comments = []
    articles_with_comments = []

    for i, article in enumerate(articles):
        try:
            if progress_callback:
                progress_callback(i, article["title"], None)

            result = fetch_all_comments(article["short_url"])
            comments = result.get("comments", [])

            # Attach article metadata to each comment
            for comment in comments:
                comment["_article_title"] = article["title"]
                comment["_article_url"] = article["url"]

            all_comments.extend(comments)

            if comments:
                articles_with_comments.append({
                    "title": article["title"],
                    "url": article["url"],
                    "commentCount": len(comments),
                })

            if progress_callback:
                progress_callback(i, article["title"], len(comments))

            # Small delay to be respectful of rate limits
            if i < len(articles) - 1:
                time.sleep(0.5)

        except Exception as e:
            print(f"Warning: Failed to fetch comments for {article['title']}: {e}")
            continue

    return {
        "articles": articles_with_comments,
        "comments": all_comments,
        "totalArticles": len(articles_with_comments),
        "totalComments": len(all_comments),
        "scrapedAt": datetime.utcnow().isoformat() + "Z",
    }


def scrape_guardian_comments(article_url: str, output_file: str = None) -> dict:
    """
    Main function to scrape comments from a Guardian article.

    Args:
        article_url: Full URL to a Guardian article
        output_file: Optional path for JSON output (default: based on article URL)

    Returns:
        Dictionary containing all comments and metadata
    """
    print(f"Scraping comments from: {article_url}")

    # Extract discussion key from article
    print("Extracting discussion key...")
    short_url = extract_short_url(article_url)
    print(f"Found discussion key: {short_url}")

    # Fetch all comments
    print("Fetching comments...")
    result = fetch_all_comments(short_url)
    result["sourceUrl"] = article_url

    # Generate output filename if not provided
    if output_file is None:
        # Create filename from article URL
        parsed = urlparse(article_url)
        path_slug = parsed.path.strip("/").replace("/", "_")[:50]
        output_file = f"comments_{path_slug}.json"

    # Save to file
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {result['totalFetched']} comments to: {output_file}")

    return result


class _FigureParser(HTMLParser):
    """SAX-style parser that extracts <figure> blocks with their img and figcaption."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.figures = []
        self._in_figure = False
        self._in_figcaption = False
        self._depth = 0          # nesting depth while inside a figure
        self._current = None
        self._caption_parts = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        if tag == "figure":
            self._in_figure = True
            self._depth = 0
            self._current = {"src": None, "alt": None, "caption": ""}
            self._caption_parts = []
        elif self._in_figure:
            self._depth += 1
            if tag == "img" and self._current is not None and self._current["src"] is None:
                self._current["src"] = attr_dict.get("src") or attr_dict.get("data-src")
                # alt attribute may be explicitly empty string — preserve that
                if "alt" in attr_dict:
                    self._current["alt"] = attr_dict["alt"]
            elif tag == "figcaption":
                self._in_figcaption = True

    def handle_endtag(self, tag):
        if tag == "figure" and self._in_figure:
            if self._current and self._current["src"]:
                self._current["caption"] = " ".join(self._caption_parts).strip()
                self.figures.append(self._current)
            self._in_figure = False
            self._in_figcaption = False
            self._current = None
            self._caption_parts = []
            self._depth = 0
        elif self._in_figure:
            self._depth -= 1
            if tag == "figcaption":
                self._in_figcaption = False

    def handle_data(self, data):
        if self._in_figcaption and data.strip():
            self._caption_parts.append(data.strip())


def scrape_article_images(article_url: str) -> dict:
    """
    Scrape all images with alt text and captions from a Guardian article page.

    Args:
        article_url: Full URL to a Guardian article

    Returns:
        Dict with keys: headline, section, article_url, images (list of dicts).
        Each image dict: src, alt, caption, position (0-indexed), is_lead.
    """
    response = requests.get(article_url, timeout=30)
    response.raise_for_status()
    html = response.text

    # Extract headline from JSON-LD (most reliable)
    headline = "Unknown Article"
    ld_match = re.search(r'"headline"\s*:\s*"([^"]+)"', html)
    if ld_match:
        headline = ld_match.group(1)
    else:
        h1_match = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
        if h1_match:
            headline = h1_match.group(1).strip()

    # Extract section from JSON-LD or meta tag
    section = ""
    section_match = re.search(r'"articleSection"\s*:\s*"([^"]+)"', html)
    if section_match:
        section = section_match.group(1)
    else:
        meta_match = re.search(r'<meta[^>]+property="article:section"[^>]+content="([^"]+)"', html)
        if meta_match:
            section = meta_match.group(1)

    # Parse figures using stdlib HTMLParser
    parser = _FigureParser()
    parser.feed(html)
    raw_figures = parser.figures

    # Deduplicate by src, skip SVGs and data URIs
    seen_srcs = set()
    images = []
    for fig in raw_figures:
        src = fig.get("src", "")
        if not src or src.startswith("data:") or src.lower().endswith(".svg"):
            continue
        if src in seen_srcs:
            continue
        seen_srcs.add(src)

        caption = fig.get("caption", "")
        # Truncate very long captions
        if len(caption) > 500:
            caption = caption[:500] + "..."

        images.append({
            "position": len(images),
            "src": src,
            "alt": fig.get("alt"),          # None means attr absent; "" means explicitly empty
            "caption": caption,
            "is_lead": len(images) == 0,
        })

    return {
        "headline": headline,
        "section": section,
        "article_url": article_url,
        "images": images,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python guardian_scraper.py <article_url> [output_file.json]")
        print("\nExample:")
        print("  python guardian_scraper.py https://www.theguardian.com/world/2024/...")
        sys.exit(1)

    article_url = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        scrape_guardian_comments(article_url, output_file)
    except requests.RequestException as e:
        print(f"Error fetching data: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
