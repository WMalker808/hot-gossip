#!/usr/bin/env python3
"""
Alt Text Quality Analyzer
Fetches Guardian article images and assesses alt text quality using Claude vision API.
"""

import base64
import json
import os
import re
import sys

import requests
from anthropic import Anthropic

from prompts import ALT_TEXT_ASSESSMENT

client = None


def _get_client():
    global client
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("Error: ANTHROPIC_API_KEY environment variable not set.")
            sys.exit(1)
        client = Anthropic(api_key=api_key)
    return client


def _resize_guardian_cdn_url(src: str) -> str:
    """
    Guardian CDN URLs follow the pattern:
        https://media.guim.co.uk/<hash>/<crop>/<width>.jpg
    Substitute the width segment with 1200 to fetch a reasonably sized image.
    Falls back to appending ?width=1200 for other URL shapes.
    """
    # Pattern: ends in /<digits>.jpg (or .jpeg / .png / .webp)
    cdn_match = re.match(
        r"(https://media\.guim\.co\.uk/.+/)(\d+)(\.(jpg|jpeg|png|webp))(\?.*)?$",
        src,
        re.IGNORECASE,
    )
    if cdn_match:
        return cdn_match.group(1) + "1200" + cdn_match.group(3)

    # For other Guardian image URLs, append a width parameter
    if "theguardian.com" in src or "guim.co.uk" in src:
        separator = "&" if "?" in src else "?"
        return src + separator + "width=1200"

    return src


def fetch_and_encode_image(src_url: str, max_size_bytes: int = 4_000_000):
    """
    Fetch an image URL and return (base64_data, media_type), or None on failure.

    Applies Guardian CDN resizing before fetching to avoid downloading originals.
    """
    resized_url = _resize_guardian_cdn_url(src_url)

    try:
        resp = requests.get(resized_url, timeout=15, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        # Only accept formats Claude supports
        if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            content_type = "image/jpeg"

        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_size_bytes:
                return None  # Image too large even after resize
            chunks.append(chunk)

        image_bytes = b"".join(chunks)
        encoded = base64.standard_b64encode(image_bytes).decode("utf-8")
        return encoded, content_type

    except Exception:
        return None


def assess_alt_text(
    image_data: dict,
    base64_image: str,
    media_type: str,
    article_headline: str,
    article_section: str,
) -> dict:
    """
    Send a single image to Claude vision API with its alt text and caption.
    Returns a structured assessment dict.
    """
    alt = image_data.get("alt")  # None = attribute absent; "" = explicitly empty
    alt_text_for_prompt = alt if alt is not None else ""
    char_count = len(alt_text_for_prompt)

    prompt = ALT_TEXT_ASSESSMENT.format(
        article_headline=article_headline,
        article_section=article_section or "Unknown",
        position=image_data["position"] + 1,
        is_lead="yes" if image_data.get("is_lead") else "no",
        alt_text=alt_text_for_prompt,
        caption=image_data.get("caption") or "",
        char_count=char_count,
    )

    api_client = _get_client()

    response = api_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64_image,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    raw = response.content[0].text
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            result = {
                "score": "fail",
                "issues": ["Could not parse Claude response"],
                "suggested_alt": "",
                "violated_criteria": [],
                "criteria_results": {},
                "is_decorative": False,
            }

    return {
        "position": image_data["position"] + 1,
        "src": image_data["src"],
        "original_alt": alt,          # preserve None vs "" distinction for display
        "alt_missing": alt is None,   # True if the attribute was absent entirely
        "caption": image_data.get("caption") or "",
        "char_count": char_count,
        "fetch_failed": False,
        "score": result.get("score", "fail"),
        "issues": result.get("issues", []),
        "suggested_alt": result.get("suggested_alt", ""),
        "violated_criteria": result.get("violated_criteria", []),
        "criteria_results": result.get("criteria_results", {}),
        "is_decorative": result.get("is_decorative", False),
    }


def assess_article_images(article_data: dict, progress_callback=None) -> list:
    """
    Assess all images from scrape_article_images() output.

    Args:
        article_data: output of scrape_article_images()
        progress_callback: optional callable(current_index, total, image_src)

    Returns:
        List of assessment result dicts, one per image.
    """
    images = article_data.get("images", [])
    headline = article_data.get("headline", "")
    section = article_data.get("section", "")
    results = []

    for i, image in enumerate(images):
        if progress_callback:
            progress_callback(i, len(images), image.get("src", ""))

        encoded = fetch_and_encode_image(image["src"])

        if encoded is None:
            results.append({
                "position": image["position"] + 1,
                "src": image["src"],
                "original_alt": image.get("alt"),
                "alt_missing": image.get("alt") is None,
                "caption": image.get("caption") or "",
                "char_count": len(image.get("alt") or ""),
                "fetch_failed": True,
                "score": "fail",
                "issues": ["Image could not be fetched for analysis"],
                "suggested_alt": "",
                "violated_criteria": [],
                "criteria_results": {},
                "is_decorative": False,
            })
        else:
            base64_data, media_type = encoded
            result = assess_alt_text(
                image_data=image,
                base64_image=base64_data,
                media_type=media_type,
                article_headline=headline,
                article_section=section,
            )
            results.append(result)

    return results
