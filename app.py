#!/usr/bin/env python3
"""
Guardian Comments Analyzer — Flask Web App
Provides a web interface for scraping and analyzing Guardian article comments.
"""

import json
import os

from flask import Flask, Response, render_template, request

from guardian_scraper import (
    extract_short_url,
    fetch_all_comments,
    search_articles_by_keyword,
    fetch_comments_for_articles,
    extract_articles_from_section,
)
from comment_analyzer import (
    prepare_comments_for_analysis,
    analyze_sentiment,
    generate_discussion_questions,
    extract_commercial_opportunities,
    extract_commercial_opportunities_aggregated,
    merge_commercial_results,
)

app = Flask(__name__)


def format_sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def generate_analysis(article_url: str):
    """Generator that yields SSE events as scraping and analysis progresses."""
    try:
        # Check API key before starting analysis
        if not os.environ.get("ANTHROPIC_API_KEY"):
            yield format_sse({
                "type": "error",
                "message": "ANTHROPIC_API_KEY environment variable not set. Set it before starting the server.",
            })
            return

        # Phase 1: Scrape comments
        yield format_sse({"type": "progress", "step": "scraping", "message": "Extracting discussion key..."})
        short_url = extract_short_url(article_url)

        yield format_sse({"type": "progress", "step": "scraping", "message": f"Found key {short_url}. Fetching comments..."})
        result = fetch_all_comments(short_url)

        comments = result["comments"]
        discussion = result.get("discussion", {})
        article_title = discussion.get("title", "Unknown Article")
        total = len(comments)

        yield format_sse({"type": "progress", "step": "scraping", "message": f"Scraped {total} comments"})

        # Send metadata immediately
        meta = {
            "articleTitle": article_title,
            "articleUrl": article_url,
            "totalComments": total,
            "uniqueCommenters": len(set(
                c.get("userProfile", {}).get("userId", "") for c in comments
            )),
            "commentsAnalyzed": min(total, 200),
        }
        yield format_sse({"type": "result", "section": "meta", "data": meta})

        if total == 0:
            yield format_sse({"type": "complete", "message": "No comments to analyze."})
            return

        # Phase 2: Prepare comments
        yield format_sse({"type": "progress", "step": "preparing", "message": "Preparing comments for analysis..."})
        comments_text = prepare_comments_for_analysis(comments)

        # Phase 3: Run four analyses sequentially
        yield format_sse({"type": "progress", "step": "analyzing", "message": "Generating discussion questions..."})
        questions = generate_discussion_questions(comments_text, article_title)
        yield format_sse({"type": "result", "section": "discussionQuestions", "data": questions})

        yield format_sse({"type": "progress", "step": "analyzing", "message": "Analyzing sentiment..."})
        sentiment = analyze_sentiment(comments_text, article_title)
        yield format_sse({"type": "result", "section": "sentiment", "data": sentiment})

        yield format_sse({"type": "progress", "step": "analyzing", "message": "Extracting commercial opportunities..."})
        try:
            commercial = extract_commercial_opportunities(comments_text, article_title)
        except Exception:
            commercial = {"brands": [], "recommendations": [], "opportunities": []}
        yield format_sse({"type": "result", "section": "commercialOpportunities", "data": commercial})

        yield format_sse({"type": "complete", "message": "Analysis complete"})

    except ValueError as e:
        yield format_sse({"type": "error", "message": str(e)})
    except Exception as e:
        yield format_sse({"type": "error", "message": f"Error: {e}"})


@app.route("/")
def index():
    response = Response(render_template("index.html"), content_type="text/html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/analyze")
def analyze():
    url = request.args.get("url", "").strip()
    if not url:
        return Response(
            format_sse({"type": "error", "message": "No URL provided"}),
            content_type="text/event-stream",
        )
    if "theguardian.com" not in url:
        return Response(
            format_sse({"type": "error", "message": "Please provide a Guardian article URL"}),
            content_type="text/event-stream",
        )

    return Response(
        generate_analysis(url),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def generate_keyword_analysis(keyword: str, limit: int):
    """Generator that yields SSE events for keyword-based commercial analysis."""
    try:
        # Check API keys
        if not os.environ.get("ANTHROPIC_API_KEY"):
            yield format_sse({
                "type": "error",
                "message": "ANTHROPIC_API_KEY environment variable not set.",
            })
            return

        if not os.environ.get("GUARDIAN_API_KEY"):
            yield format_sse({
                "type": "error",
                "message": "GUARDIAN_API_KEY environment variable not set.",
            })
            return

        # Phase 1: Search for articles
        yield format_sse({
            "type": "progress",
            "step": "searching",
            "message": f"Searching for articles about '{keyword}'...",
        })

        articles = search_articles_by_keyword(keyword, limit)

        if not articles:
            yield format_sse({
                "type": "error",
                "message": f"No articles found for '{keyword}'",
            })
            return

        yield format_sse({
            "type": "progress",
            "step": "searching",
            "message": f"Found {len(articles)} articles",
        })

        # Send article list
        yield format_sse({
            "type": "result",
            "section": "articles",
            "data": articles,
        })

        # Phase 2: Scrape comments from each article
        all_comments = []
        articles_with_comments = []

        for i, article in enumerate(articles):
            yield format_sse({
                "type": "progress",
                "step": "scraping",
                "message": f"Scraping comments from article {i + 1}/{len(articles)}: {article['title'][:50]}...",
            })

            try:
                result = fetch_all_comments(article["short_url"])
                comments = result.get("comments", [])

                for comment in comments:
                    comment["_article_title"] = article["title"]

                all_comments.extend(comments)

                if comments:
                    articles_with_comments.append({
                        "title": article["title"],
                        "url": article["url"],
                        "commentCount": len(comments),
                    })

            except Exception as e:
                yield format_sse({
                    "type": "progress",
                    "step": "scraping",
                    "message": f"Warning: Could not fetch comments for article {i + 1}",
                })

        if not all_comments:
            yield format_sse({
                "type": "error",
                "message": "No comments found across any articles",
            })
            return

        # Send metadata
        meta = {
            "keyword": keyword,
            "articlesSearched": len(articles),
            "articlesWithComments": len(articles_with_comments),
            "totalComments": len(all_comments),
            "commentsAnalyzed": min(len(all_comments), 200),
        }
        yield format_sse({"type": "result", "section": "meta", "data": meta})

        # Phase 3: Analyze comments in batches
        total_comments = len(all_comments)
        batch_size = 200

        # Sort by recommendations to prioritize high-value comments
        sorted_comments = sorted(all_comments, key=lambda c: c.get('numRecommends', 0), reverse=True)

        # Split into batches
        batches = [sorted_comments[i:i + batch_size] for i in range(0, len(sorted_comments), batch_size)]
        num_batches = len(batches)

        yield format_sse({
            "type": "progress",
            "step": "analyzing",
            "message": f"Analyzing {total_comments} comments in {num_batches} batch(es)...",
        })

        batch_results = []
        for i, batch in enumerate(batches):
            yield format_sse({
                "type": "progress",
                "step": "analyzing",
                "message": f"Processing batch {i + 1}/{num_batches} ({len(batch)} comments)...",
            })

            comments_text = prepare_comments_for_analysis(batch, max_comments=len(batch))
            result = extract_commercial_opportunities_aggregated(
                comments_text, keyword, len(articles_with_comments)
            )
            batch_results.append(result)

        # Merge all batch results
        commercial = merge_commercial_results(batch_results)

        yield format_sse({
            "type": "result",
            "section": "commercialOpportunities",
            "data": commercial,
        })

        yield format_sse({"type": "complete", "message": "Analysis complete"})

    except ValueError as e:
        yield format_sse({"type": "error", "message": str(e)})
    except Exception as e:
        yield format_sse({"type": "error", "message": f"Error: {e}"})


@app.route("/analyze-keyword")
def analyze_keyword():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return Response(
            format_sse({"type": "error", "message": "No keyword provided"}),
            content_type="text/event-stream",
        )

    try:
        limit = int(request.args.get("limit", 10))
        limit = max(1, min(limit, 20))  # Clamp to 1-20
    except ValueError:
        limit = 10

    return Response(
        generate_keyword_analysis(keyword, limit),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def generate_section_analysis(section_url: str, limit: int):
    """Generator that yields SSE events for section-based commercial analysis."""
    try:
        # Check API key
        if not os.environ.get("ANTHROPIC_API_KEY"):
            yield format_sse({
                "type": "error",
                "message": "ANTHROPIC_API_KEY environment variable not set.",
            })
            return

        # Phase 1: Extract articles from section page
        yield format_sse({
            "type": "progress",
            "step": "searching",
            "message": f"Extracting articles from section page...",
        })

        articles = extract_articles_from_section(section_url, limit)

        if not articles:
            yield format_sse({
                "type": "error",
                "message": "No articles with comments found on this page",
            })
            return

        yield format_sse({
            "type": "progress",
            "step": "searching",
            "message": f"Found {len(articles)} articles with comments",
        })

        # Send article list
        yield format_sse({
            "type": "result",
            "section": "articles",
            "data": articles,
        })

        # Phase 2: Scrape comments from each article
        all_comments = []
        articles_with_comments = []

        for i, article in enumerate(articles):
            yield format_sse({
                "type": "progress",
                "step": "scraping",
                "message": f"Scraping comments from article {i + 1}/{len(articles)}: {article['title'][:50]}...",
            })

            try:
                result = fetch_all_comments(article["short_url"])
                comments = result.get("comments", [])

                for comment in comments:
                    comment["_article_title"] = article["title"]

                all_comments.extend(comments)

                if comments:
                    articles_with_comments.append({
                        "title": article["title"],
                        "url": article["url"],
                        "commentCount": len(comments),
                    })

            except Exception as e:
                yield format_sse({
                    "type": "progress",
                    "step": "scraping",
                    "message": f"Warning: Could not fetch comments for article {i + 1}",
                })

        if not all_comments:
            yield format_sse({
                "type": "error",
                "message": "No comments found across any articles",
            })
            return

        # Extract section name from URL for context
        section_name = section_url.rstrip("/").split("/")[-1].replace("-", " ").title()

        # Send metadata
        meta = {
            "sectionUrl": section_url,
            "sectionName": section_name,
            "articlesFound": len(articles),
            "articlesWithComments": len(articles_with_comments),
            "totalComments": len(all_comments),
            "commentsAnalyzed": len(all_comments),
        }
        yield format_sse({"type": "result", "section": "meta", "data": meta})

        # Phase 3: Analyze comments in batches
        total_comments = len(all_comments)
        batch_size = 200

        sorted_comments = sorted(all_comments, key=lambda c: c.get('numRecommends', 0), reverse=True)
        batches = [sorted_comments[i:i + batch_size] for i in range(0, len(sorted_comments), batch_size)]
        num_batches = len(batches)

        yield format_sse({
            "type": "progress",
            "step": "analyzing",
            "message": f"Analyzing {total_comments} comments in {num_batches} batch(es)...",
        })

        batch_results = []
        for i, batch in enumerate(batches):
            yield format_sse({
                "type": "progress",
                "step": "analyzing",
                "message": f"Processing batch {i + 1}/{num_batches} ({len(batch)} comments)...",
            })

            comments_text = prepare_comments_for_analysis(batch, max_comments=len(batch))
            result = extract_commercial_opportunities_aggregated(
                comments_text, section_name, len(articles_with_comments)
            )
            batch_results.append(result)

        commercial = merge_commercial_results(batch_results)

        yield format_sse({
            "type": "result",
            "section": "commercialOpportunities",
            "data": commercial,
        })

        yield format_sse({"type": "complete", "message": "Analysis complete"})

    except ValueError as e:
        yield format_sse({"type": "error", "message": str(e)})
    except Exception as e:
        yield format_sse({"type": "error", "message": f"Error: {e}"})


@app.route("/analyze-section")
def analyze_section():
    url = request.args.get("url", "").strip()
    if not url:
        return Response(
            format_sse({"type": "error", "message": "No section URL provided"}),
            content_type="text/event-stream",
        )
    if "theguardian.com" not in url:
        return Response(
            format_sse({"type": "error", "message": "Please provide a Guardian section URL"}),
            content_type="text/event-stream",
        )

    try:
        limit = int(request.args.get("limit", 10))
        limit = max(1, min(limit, 20))
    except ValueError:
        limit = 10

    return Response(
        generate_section_analysis(url, limit),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
