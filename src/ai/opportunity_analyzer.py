"""Business opportunity analysis using AI.

A second scoring pass alongside ContentAnalyzer: instead of judging news
importance, it judges whether an item reveals a buildable product
opportunity (BuilderPulse-style). Results are stored in
``item.metadata["opportunity"]`` and never overwrite the news-analysis
fields (``ai_score`` / ``ai_reason`` / ``ai_summary`` / ``ai_tags``).
"""

import asyncio
from typing import List, Optional
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn

from .client import AIClient
from .prompts import OPPORTUNITY_ANALYSIS_SYSTEM, OPPORTUNITY_ANALYSIS_USER
from .utils import parse_json_response
from ..models import ContentItem

DEFAULT_THROTTLE_SEC = 0.0

LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Simplified Chinese (简体中文)",
}


class OpportunityResult(BaseModel):
    """Validated structured result returned by the opportunity model."""

    score: float = Field(ge=0, le=10, allow_inf_nan=False)
    reason: str
    problem: str
    target_user: str
    mvp: str
    distribution: str
    monetization: str
    counter_view: str
    validation_step: str
    tags: list[str]


class OpportunityAnalyzer:
    """Scores content items for business opportunity potential."""

    def __init__(self, ai_client: AIClient):
        self.client = ai_client

    def _get_throttle_sec(self) -> float:
        """Return the configured inter-item throttle, clamped to zero or above."""
        config = getattr(self.client, "config", None)
        throttle_sec = getattr(config, "throttle_sec", DEFAULT_THROTTLE_SEC)
        return max(throttle_sec, 0.0)

    def _get_concurrency(self) -> int:
        """Return the configured analysis concurrency, clamped to 1 or above."""
        config = getattr(self.client, "config", None)
        concurrency = getattr(config, "analysis_concurrency", 1)
        return max(concurrency, 1)

    def _get_language_instruction(self) -> str:
        """Build an output-language instruction from the configured languages."""
        config = getattr(self.client, "config", None)
        languages = getattr(config, "languages", None) or ["en"]
        names = [LANGUAGE_NAMES.get(lang, lang) for lang in languages]
        if names == [LANGUAGE_NAMES["en"]]:
            return "Write all free-text fields in English."
        return f"Write all free-text fields (reason, problem, target_user, mvp, distribution, monetization, counter_view, validation_step) in {', '.join(names)}. Keep technical terms and proper nouns in their original form."

    async def analyze_batch(self, items: List[ContentItem]) -> List[ContentItem]:
        throttle_sec = self._get_throttle_sec()
        concurrency = self._get_concurrency()
        semaphore = asyncio.Semaphore(concurrency)

        async def _process(item: ContentItem, index: int, progress_task) -> ContentItem:
            async with semaphore:
                try:
                    await self._analyze_item(item)
                except Exception as e:
                    print(f"Error analyzing opportunity for item {item.id}: {e}")
                if throttle_sec > 0 and index < len(items) - 1:
                    await asyncio.sleep(throttle_sec)
            progress.advance(progress_task)
            return item

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Scoring opportunities", total=len(items))
            coros = [
                _process(item, i, task) for i, item in enumerate(items)
            ]
            analyzed_items = await asyncio.gather(*coros)

        return analyzed_items

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10)
    )
    async def _analyze_item(self, item: ContentItem) -> None:
        """Score a single content item for opportunity potential.

        Args:
            item: Content item to analyze (modified in-place)
        """
        # Prepare content section
        content_section = ""
        if item.content:
            content_text = item.content
            if "--- Top Comments ---" in content_text:
                main, _ = content_text.split("--- Top Comments ---", 1)
                content_section = f"Content: {main.strip()[:800]}"
            else:
                content_section = f"Content: {content_text[:1000]}"

        # Prepare discussion section (comments, engagement)
        discussion_parts = []
        if item.content and "--- Top Comments ---" in item.content:
            comments_part = item.content.split("--- Top Comments ---", 1)[1]
            discussion_parts.append(f"Community Comments:\n{comments_part[:1500]}")

        meta = item.metadata
        engagement_items = []
        if meta.get("score"):
            engagement_items.append(f"score: {meta['score']}")
        if meta.get("descendants"):
            engagement_items.append(f"{meta['descendants']} comments")
        if meta.get("favorite_count"):
            engagement_items.append(f"{meta['favorite_count']} likes")
        if meta.get("retweet_count"):
            engagement_items.append(f"{meta['retweet_count']} retweets")
        if meta.get("reply_count"):
            engagement_items.append(f"{meta['reply_count']} replies")
        if meta.get("views"):
            engagement_items.append(f"{meta['views']} views")
        if meta.get("upvote_ratio"):
            engagement_items.append(f"upvote ratio: {meta['upvote_ratio']:.0%}")
        if engagement_items:
            discussion_parts.append(f"Engagement: {', '.join(engagement_items)}")
        if meta.get("discussion_url"):
            discussion_parts.append(f"Discussion: {meta['discussion_url']}")

        discussion_section = "\n".join(discussion_parts) if discussion_parts else ""

        # Generate user prompt
        user_prompt = OPPORTUNITY_ANALYSIS_USER.format(
            title=item.title,
            source=f"{item.source_type.value}",
            author=item.author or "Unknown",
            url=str(item.url),
            content_section=content_section,
            discussion_section=discussion_section,
            language_instruction=self._get_language_instruction(),
        )

        # Get AI completion
        response = await self.client.complete(
            system=OPPORTUNITY_ANALYSIS_SYSTEM,
            user=user_prompt,
        )

        # Parse JSON response with robust fallback
        parsed = parse_json_response(response)
        try:
            result = OpportunityResult.model_validate(parsed) if parsed is not None else None
        except ValidationError:
            result = None
        if result is None:
            print(f"Warning: could not parse opportunity response for {item.id}, skipping")
            return

        # Store results under metadata; never overwrite the news-analysis fields
        item.metadata["opportunity"] = result.model_dump()
