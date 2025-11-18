#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
duplicate_title_cleanup.py

Example script for GitHub:
- Fetches WordPress posts via REST API
- Uses DeepSeek to detect groups of possibly duplicate titles
- Lets the user decide per group:
  - keep one post and delete the others, or
  - skip the group, or
  - cancel completely

By default this script does not delete anything.
To actually delete posts you must run it with the --delete flag.

Required environment variables:
- DEEPSEEK_API_KEY   DeepSeek API key
- DEEPSEEK_BASE      Optional, default: https://api.deepseek.com
- WP_BASE            WordPress base URL, for example: https://example.com
- WP_USER            WordPress application username
- WP_APP_PASSWORD    WordPress application password
"""

import os
import re
import sys
import json
import time
import base64
import argparse
from typing import Dict, Any, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter, Retry

# ===== Console colors =====
BLUE = "\033[94m"
RED = "\033[91m"
DARKGREEN = "\033[32m"
GRAY = "\033[90m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def blue(s: str) -> str:
    return f"{BLUE}{s}{RESET}"


def dgreen(s: str) -> str:
    return f"{DARKGREEN}{s}{RESET}"


def gray(s: str) -> str:
    return f"{GRAY}{s}{RESET}"


def info(s: str) -> None:
    print(blue(s))


def ok(s: str) -> None:
    print(dgreen(s))


def warn(s: str) -> None:
    print(f"{YELLOW}{s}{RESET}")


def err(s: str) -> None:
    print(f"{RED}{s}{RESET}", file=sys.stderr)


# ===== Credentials via environment =====
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com")

WP_BASE = (os.getenv("WP_BASE") or "").rstrip("/")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

if not DEEPSEEK_API_KEY:
    err("Missing DEEPSEEK_API_KEY environment variable.")
    sys.exit(1)

if not WP_BASE or not WP_USER or not WP_APP_PASSWORD:
    err("Missing WordPress configuration. Set WP_BASE, WP_USER, and WP_APP_PASSWORD.")
    sys.exit(1)

WP_POSTS_URL = f"{WP_BASE}/wp-json/wp/v2/posts"


# ===== HTTP Session =====
def make_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST", "DELETE"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update(
        {
            "Authorization": "Basic "
            + base64.b64encode(f"{WP_USER}:{WP_APP_PASSWORD}".encode()).decode()
        }
    )
    return s


# ===== Helpers =====
def sanitize_text(v: str) -> str:
    v = v or ""
    v = re.sub(r"<[^>]+>", " ", v)
    v = re.sub(r'[|\\"“”‘’]+', "", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v


# ===== DeepSeek wrapper =====
def deepseek_chat_json(
    system_prompt: str, user_prompt: str, temperature: float = 0.2, timeout: int = 60
) -> Dict[str, Any]:
    url = f"{DEEPSEEK_BASE}/v1/chat/completions"
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    content = (data["choices"][0]["message"]["content"] or "").strip()
    content = content.strip().strip("`").strip()
    if content.lower().startswith("json"):
        content = content[4:].strip()
    return json.loads(content)


# ===== WordPress: fetch posts =====
def fetch_all_posts(
    sess: requests.Session, per_page: int = 100, max_posts: Optional[int] = None
) -> List[Dict[str, Any]]:
    all_posts: List[Dict[str, Any]] = []
    page = 1
    while True:
        params = {
            "per_page": per_page,
            "page": page,
            "status": "publish,future",
            "_fields": "id,title,link,status,date,featured_media",
        }
        r = sess.get(WP_POSTS_URL, params=params, timeout=60)
        if r.status_code == 400 and "rest_post_invalid_page_number" in r.text:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break

        info(f"Fetched page {page} with {len(batch)} posts")
        for p in batch:
            pid = int(p["id"])
            title_html = (p.get("title") or {}).get("rendered") or ""
            title_clean = sanitize_text(title_html)
            link = p.get("link") or f"{WP_BASE}/?p={pid}"
            status = p.get("status", "")
            date = p.get("date", "")
            featured_media = p.get("featured_media", 0)

            all_posts.append(
                {
                    "id": pid,
                    "title": title_clean,
                    "link": link,
                    "status": status,
                    "date": date,
                    "featured_media": featured_media,
                }
            )

            if max_posts is not None and len(all_posts) >= max_posts:
                info(f"Reached max_posts={max_posts}, stopping fetch.")
                return all_posts

        page += 1
        time.sleep(0.2)

    return all_posts


# ===== DeepSeek grouping =====
def group_titles_with_deepseek(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Expected response:

    {
      "groups": [
        {
          "reason": "short explanation",
          "post_ids": [123, 456]
        },
        ...
      ]
    }

    Each post id may appear in at most one group.
    Groups with fewer than two distinct ids are ignored.
    """
    if not posts:
        return []

    payload_posts = [{"id": p["id"], "title": p["title"]} for p in posts]

    system_prompt = (
        "You analyze WordPress post titles and identify groups of titles "
        "that are likely duplicates. Titles belong to the same group if they clearly "
        "describe the same topic or article, even if wording or small details differ. "
        "Be conservative, only create a group when duplication is quite plausible. "
        "Do not include singletons. Each post id may appear in at most one group. "
        "Return strict JSON only, no markdown, with this structure:\n"
        "{\n"
        '  \"groups\": [\n'
        '    {\"reason\": \"short explanation\", \"post_ids\": [id1, id2, ...]},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
    )

    user_prompt_dict = {
        "instruction": "Group possibly duplicate titles by post id. Skip titles that have no duplicates.",
        "posts": payload_posts,
    }
    user_prompt = json.dumps(user_prompt_dict, ensure_ascii=False)

    info("Sending titles to DeepSeek for duplicate grouping...")
    data = deepseek_chat_json(system_prompt, user_prompt, temperature=0.1, timeout=120)

    if not isinstance(data, dict):
        warn("DeepSeek response is not a dict. No groups returned.")
        return []

    groups = data.get("groups") or []
    if not isinstance(groups, list):
        warn("DeepSeek 'groups' field is not a list. No groups returned.")
        return []

    valid_groups: List[Dict[str, Any]] = []
    used_ids = set()
    for g in groups:
        if not isinstance(g, dict):
            continue
        reason = str(g.get("reason", "")).strip()
        ids = g.get("post_ids")
        if not isinstance(ids, list):
            continue
        cleaned_ids = []
        for pid in ids:
            try:
                pid_int = int(pid)
            except (ValueError, TypeError):
                continue
            if pid_int in used_ids:
                continue
            used_ids.add(pid_int)
            cleaned_ids.append(pid_int)
        if len(cleaned_ids) >= 2:
            valid_groups.append({"reason": reason, "post_ids": cleaned_ids})

    return valid_groups


# ===== WordPress: delete post =====
def delete_post(sess: requests.Session, post_id: int) -> bool:
    url = f"{WP_POSTS_URL}/{post_id}"
    params = {"force": "true"}
    try:
        r = sess.delete(url, params=params, timeout=60)
        if r.status_code == 410:
            warn(f"Post {post_id} already deleted or gone (HTTP 410).")
            return True
        if not r.ok:
            err(f"Failed to delete post {post_id}: HTTP {r.status_code} - {r.text[:200]}")
            return False
        ok(f"Deleted post {post_id}.")
        return True
    except Exception as e:
        err(f"Error while deleting post {post_id}: {e}")
        return False


# ===== Group output and user interaction =====
def print_group_and_ask_choice(
    group: Dict[str, Any], posts: List[Dict[str, Any]]
) -> Tuple[str, Optional[int]]:
    """
    Returns:
        ("keep", post_id)  -> keep that post and delete others
        ("skip", None)     -> skip this group, do not delete anything
        ("cancel", None)   -> cancel and exit script
    """
    by_id: Dict[int, Dict[str, Any]] = {int(p["id"]): p for p in posts}

    reason = group.get("reason", "")
    post_ids = group.get("post_ids", [])

    print()
    ok("Possible duplicate group found:")
    print("=" * 80)
    print(dgreen(f"Reason: {reason if reason else '(no reason given)'}"))
    print()

    group_posts: List[Dict[str, Any]] = []
    for idx, pid in enumerate(post_ids, start=1):
        post = by_id.get(pid)
        if not post:
            print(gray(f"{idx}) Post {pid} not found in fetched list"))
            continue

        has_feat = bool(post.get("featured_media"))
        feat_str = "YES" if has_feat else "NO"

        print(f"{idx}) ID {pid} | status: {post.get('status', '')} | date: {post.get('date', '')}")
        print(f"    Title: {post['title']}")
        print(f"    URL:   {post['link']}")
        print(f"    Featured image: {feat_str}")
        print()

        group_posts.append(post)

    if len(group_posts) < 2:
        warn("Group has fewer than two valid posts after lookup. Nothing to do for this group.")
        return "skip", None

    print("=" * 80)
    print("Choose what to do with this group.")
    print("Type the number, for example 1, to KEEP that post and delete the others in this group.")
    print("Type S to SKIP this group without any changes.")
    print("Type C or Q to CANCEL and exit the script immediately.")
    print()

    valid_indices = list(range(1, len(post_ids) + 1))
    while True:
        choice = input("Your choice (number, S, or C/Q): ").strip().lower()
        if choice in ("c", "cancel", "q", "quit"):
            warn("Operation cancelled by user. No posts will be deleted for this group.")
            return "cancel", None
        if choice in ("s", "skip"):
            ok("Skipping this group. No posts will be deleted here.")
            return "skip", None

        try:
            idx = int(choice)
        except ValueError:
            print("Please enter a valid number, S to skip, or C/Q to cancel.")
            continue
        if idx not in valid_indices:
            print(
                f"Please enter a number between 1 and {len(valid_indices)}, "
                "S to skip, or C/Q to cancel."
            )
            continue

        chosen_pid = post_ids[idx - 1]
        chosen_post = by_id.get(chosen_pid)
        print()
        info("You chose to KEEP this post:")
        print(f"  ID {chosen_pid}: {chosen_post['title'] if chosen_post else '(not found)'}")
        print()
        confirm = input("Type 'yes' to confirm, anything else to cancel this group: ").strip().lower()
        if confirm == "yes":
            return "keep", chosen_pid
        else:
            warn("Confirmation not given. No posts will be deleted for this group.")
            return "skip", None


def delete_other_posts_in_group(
    sess: requests.Session, group: Dict[str, Any], keep_post_id: int, delete_enabled: bool
) -> None:
    post_ids = group.get("post_ids", [])
    print()
    if not delete_enabled:
        warn("Delete flag is not set. This is a dry run. No posts will be deleted.")
    else:
        info("Starting deletion of other posts in this group...")

    for pid in post_ids:
        pid_int = int(pid)
        if pid_int == keep_post_id:
            ok(f"Keeping post {pid_int}.")
            continue
        if not delete_enabled:
            warn(f"[DRY RUN] Would delete post {pid_int}.")
            continue
        deleted = delete_post(sess, pid_int)
        if not deleted:
            warn(f"Could not delete post {pid_int}. Please check manually.")


# ===== main =====
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check for possible duplicate WordPress post title groups using DeepSeek and optionally delete duplicates."
    )
    parser.add_argument(
        "--per_page",
        type=int,
        default=100,
        help="Posts per REST page, default 100, max 100.",
    )
    parser.add_argument(
        "--max_posts",
        type=int,
        default=None,
        help="Optional limit for number of posts to fetch, useful for testing.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete duplicates. Without this flag the script only prints what would be deleted.",
    )
    args = parser.parse_args()

    sess = make_session()
    try:
        posts = fetch_all_posts(sess, per_page=args.per_page, max_posts=args.max_posts)
    except Exception as e:
        err(f"Failed to fetch posts: {e}")
        sys.exit(1)

    if not posts:
        warn("No posts fetched. Check API credentials and WP_BASE.")
        sys.exit(0)

    info(f"Total posts fetched: {len(posts)}")

    try:
        groups = group_titles_with_deepseek(posts)
    except Exception as e:
        err(f"DeepSeek grouping failed: {e}")
        sys.exit(1)

    if not groups:
        ok("DeepSeek did not report any duplicate groups. Nothing to do.")
        sys.exit(0)

    ok(f"Total groups reported by DeepSeek: {len(groups)}")
    if not args.delete:
        warn("Delete flag is not set. Running in dry run mode, no posts will be deleted.")
    print()

    for idx, group in enumerate(groups, start=1):
        info(f"Processing group {idx} of {len(groups)}")

        action, keep_post_id = print_group_and_ask_choice(group, posts)

        if action == "cancel":
            ok("User cancelled. Script will exit now. No further groups will be processed.")
            sys.exit(0)

        if action == "skip":
            ok("Group skipped. Moving on to the next group.")
        elif action == "keep" and keep_post_id is not None:
            delete_other_posts_in_group(sess, group, keep_post_id, delete_enabled=args.delete)

        if idx < len(groups):
            print()
            print("=" * 80)
            print("Group processed.")
            print("Press Enter to move to the next group, or type Q to quit now.")
            response = input("Continue? (Enter or Q): ").strip().lower()
            if response in ("q", "quit"):
                ok("User requested exit. Script will stop before processing further groups.")
                sys.exit(0)

    ok("All reported groups processed. Script will now exit.")


if __name__ == "__main__":
    main()
