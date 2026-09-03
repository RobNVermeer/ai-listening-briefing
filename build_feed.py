#!/usr/bin/env python3
"""Build the curated Podcast Addict RSS feed from original publisher feeds."""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
MEDIA = "http://search.yahoo.com/mrss/"
SELF_URL = "https://raw.githubusercontent.com/RobNVermeer/ai-listening-briefing/main/ai-briefing.xml"
REPO_URL = "https://github.com/RobNVermeer/ai-listening-briefing"

ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)


def text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def norm(value: str) -> str:
    value = html.unescape(value)
    value = unicodedata.normalize("NFKD", value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def fetch_xml(url: str) -> ET.Element:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Rob-AI-Listening-Briefing/1.0 (+https://github.com/RobNVermeer/ai-listening-briefing)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return ET.fromstring(response.read())


def get_channel(root: ET.Element) -> ET.Element:
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("Expected RSS <channel> but none was found")
    return channel


def find_episode(channel: ET.Element, wanted_title: str) -> ET.Element:
    wanted = norm(wanted_title)
    items = channel.findall("item")

    exact = [i for i in items if norm(text(i.find("title"))) == wanted]
    if exact:
        return exact[0]

    partial = [
        i
        for i in items
        if wanted in norm(text(i.find("title"))) or norm(text(i.find("title"))) in wanted
    ]
    if len(partial) == 1:
        return partial[0]

    recent = "\n".join(f"  - {text(i.find('title'))}" for i in items[:25])
    raise RuntimeError(
        f"Could not uniquely match episode title: {wanted_title!r}. Recent titles:\n{recent}"
    )


def get_enclosure(item: ET.Element) -> dict[str, str]:
    enclosure = item.find("enclosure")
    if enclosure is not None and enclosure.attrib.get("url"):
        return {
            "url": enclosure.attrib["url"],
            "length": enclosure.attrib.get("length", "0"),
            "type": enclosure.attrib.get("type", "audio/mpeg"),
        }

    media = item.find(f"{{{MEDIA}}}content")
    if media is not None and media.attrib.get("url"):
        return {
            "url": media.attrib["url"],
            "length": media.attrib.get("fileSize", "0"),
            "type": media.attrib.get("type", "audio/mpeg"),
        }

    raise RuntimeError(f"No playable audio enclosure for {text(item.find('title'))!r}")


def stable_guid(feed_url: str, item: ET.Element) -> str:
    original = text(item.find("guid")) or text(item.find("link")) or text(item.find("title"))
    digest = hashlib.sha256(f"{feed_url}|{original}".encode("utf-8")).hexdigest()[:24]
    return f"rob-ai-briefing-{digest}"


def original_date(item: ET.Element) -> str:
    raw = text(item.find("pubDate"))
    return raw or "Original publication date unavailable"


def build() -> None:
    config = json.loads(Path("selection.json").read_text(encoding="utf-8"))
    editions = config.get("editions", [])
    if not editions:
        raise RuntimeError("selection.json has no editions")

    # Fetch each publisher feed only once.
    feed_cache: dict[str, tuple[ET.Element, str]] = {}
    resolved: list[dict] = []

    for edition in editions:
        selections = sorted(edition["selections"], key=lambda x: x["order"])
        core_count = sum(1 for s in selections if s.get("slot", "core") == "core")
        edition_base = datetime.fromisoformat(edition["date"]).replace(
            hour=18, minute=0, second=0, tzinfo=timezone.utc
        )

        for sel in selections:
            url = sel["feed_url"]
            if url not in feed_cache:
                source_root = fetch_xml(url)
                source_channel = get_channel(source_root)
                feed_cache[url] = (source_channel, text(source_channel.find("title")) or "Podcast")

            source_channel, show_title = feed_cache[url]
            item = find_episode(source_channel, sel["episode_title"])
            enclosure = get_enclosure(item)
            duration = text(item.find(f"{{{ITUNES}}}duration"))
            link = text(item.find("link"))
            slot = sel.get("slot", "core")
            order = int(sel["order"])

            if slot == "bonus":
                prefix = "[BONUS]"
            else:
                prefix = f"[{order}/{core_count}]"

            resolved.append(
                {
                    "edition_date": edition["date"],
                    "edition_title": edition["title"],
                    "synthetic_date": edition_base - timedelta(minutes=order - 1),
                    "prefix": prefix,
                    "show_title": show_title,
                    "episode_title": text(item.find("title")),
                    "why": sel["why"],
                    "original_date": original_date(item),
                    "link": link,
                    "guid": stable_guid(url, item),
                    "enclosure": enclosure,
                    "duration": duration,
                    "slot": slot,
                }
            )

    # Newest edition first; within an edition, listening order is preserved by synthetic pubDate.
    resolved.sort(key=lambda x: x["synthetic_date"], reverse=True)

    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = config["feed_title"]
    ET.SubElement(channel, "link").text = REPO_URL
    ET.SubElement(
        channel,
        f"{{{ATOM}}}link",
        {"href": SELF_URL, "rel": "self", "type": "application/rss+xml"},
    )
    ET.SubElement(channel, "description").text = config["feed_description"]
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "generator").text = "Rob AI Listening Briefing feed builder"
    ET.SubElement(channel, "ttl").text = "60"
    newest = max(x["synthetic_date"] for x in resolved)
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(newest)
    ET.SubElement(channel, f"{{{ITUNES}}}author").text = "AI Listening Briefing"
    ET.SubElement(channel, f"{{{ITUNES}}}summary").text = (
        "A serious weekly AI listening programme curated from original podcast publishers."
    )
    ET.SubElement(channel, f"{{{ITUNES}}}explicit").text = "false"
    ET.SubElement(channel, f"{{{ITUNES}}}block").text = "yes"
    ET.SubElement(channel, f"{{{ITUNES}}}type").text = "episodic"

    for ep in resolved:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = (
            f"{ep['prefix']} {ep['episode_title']} — {ep['show_title']}"
        )
        description = (
            f"Edition: {ep['edition_date']} — {ep['edition_title']}\n\n"
            f"Why it matters: {ep['why']}\n\n"
            f"Source: {ep['show_title']}\n"
            f"Original publication: {ep['original_date']}"
        )
        ET.SubElement(item, "description").text = description
        if ep["link"]:
            ET.SubElement(item, "link").text = ep["link"]
        ET.SubElement(item, "pubDate").text = format_datetime(ep["synthetic_date"])
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = ep["guid"]
        ET.SubElement(item, "enclosure", ep["enclosure"])
        if ep["duration"]:
            ET.SubElement(item, f"{{{ITUNES}}}duration").text = ep["duration"]
        ET.SubElement(item, f"{{{ITUNES}}}explicit").text = "false"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write("ai-briefing.xml", encoding="utf-8", xml_declaration=True)
    print(f"Built ai-briefing.xml with {len(resolved)} curated episodes.")
    for ep in resolved:
        print(f"{ep['prefix']} {ep['episode_title']} — {ep['show_title']}")


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
