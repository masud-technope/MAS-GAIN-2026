#!/usr/bin/env python3
"""Fetch a GitHub pull request and extract metadata, changed files, and paired review comments.

Examples:
    python collect_pr_data.py octocat/Hello-World 1
    python collect_pr_data.py --repo-url https://github.com/octocat/Hello-World/pull/1
    python collect_pr_data.py octocat/Hello-World 1 --token $GITHUB_TOKEN --output pr.json
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class GitHubRequestError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract PR title, description, changed code, and review comments")
    parser.add_argument("repo", nargs="?", help="Repository in owner/repo format")
    parser.add_argument("pr_number", nargs="?", type=int, help="Pull request number")
    parser.add_argument("--repo-url", help="GitHub pull request URL instead of repo/pr_number")
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"), help="GitHub token (optional)")
    parser.add_argument("--output", help="Optional JSON output file path")
    parser.add_argument("--report-name", default="default", help="Dataset report folder name (default: default)")
    return parser.parse_args()


def parse_repo_url(pr_url: str) -> Tuple[str, int]:
    parsed = urllib.parse.urlparse(pr_url)
    path = parsed.path.strip("/")
    match = None
    if parsed.netloc.lower().endswith("github.com"):
        match = __import__("re").match(r"^([^/]+)/([^/]+)/(?:pull|pulls)/([0-9]+)(?:/.*)?$", path)
    if not match:
        raise ValueError(f"Unsupported GitHub PR URL: {pr_url}")
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def make_request(url: str, token: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> Any:
    if params:
        query = urllib.parse.urlencode(params)
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pr-data-extractor/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise GitHubRequestError(f"GitHub API request failed ({exc.code}): {body}") from exc
    except urllib.error.URLError as exc:
        raise GitHubRequestError(f"Network error while calling GitHub API: {exc}") from exc


def fetch_paginated_json(url: str, token: Optional[str] = None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    page = 1
    while True:
        payload = make_request(url, token=token, params={"per_page": 100, "page": page})
        if not isinstance(payload, list):
            if payload:
                results.append(payload)
            break
        if not payload:
            break
        results.extend(payload)
        if len(payload) < 100:
            break
        page += 1
    return results


def extract_changed_code(patch: Optional[str]) -> str:
    if not patch:
        return ""
    lines: List[str] = []
    for line in patch.splitlines():
        if line.startswith(("@@", "---", "+++")):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            lines.append(line[1:])
        elif line.startswith(" "):
            lines.append(line[1:])
    return "\n".join(lines).strip()


def extract_code_for_line(patch: Optional[str], line_number: Optional[int]) -> str:
    if not patch or line_number is None:
        return extract_changed_code(patch)

    current_hunk_lines: List[str] = []
    current_hunk_start: Optional[int] = None
    current_new_line: Optional[int] = None

    for line in patch.splitlines():
        if line.startswith("@@"):
            if current_hunk_start is not None and current_hunk_lines and current_hunk_start <= line_number < (current_new_line or current_hunk_start):
                return "\n".join(current_hunk_lines).strip()
            current_hunk_lines = []
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            current_hunk_start = int(match.group(1)) if match else None
            current_new_line = current_hunk_start
            continue

        if current_hunk_start is None or line.startswith(("---", "+++")):
            continue

        if line.startswith("+") and not line.startswith("+++"):
            current_hunk_lines.append(line[1:])
            if current_new_line is not None and current_new_line == line_number:
                return "\n".join(current_hunk_lines).strip()
            current_new_line = (current_new_line or current_hunk_start) + 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            current_hunk_lines.append(line[1:])
            if current_new_line is not None and current_new_line == line_number:
                return "\n".join(current_hunk_lines).strip()
            current_new_line = (current_new_line or current_hunk_start) + 1

    if current_hunk_start is not None and current_hunk_lines and current_hunk_start <= (line_number or 0) < (current_new_line or current_hunk_start):
        return "\n".join(current_hunk_lines).strip()
    return extract_changed_code(patch)


def build_file_url(repo: str, pr_number: int, file_index: int) -> str:
    return f"https://github.com/{repo}/pull/{pr_number}/files#diff-{file_index}"


def pair_comments_with_code(files: List[Dict[str, Any]], review_comments: List[Dict[str, Any]], repo: str, pr_number: int) -> List[Dict[str, Any]]:
    file_lookup = {entry["path"]: entry for entry in files if entry.get("path")}
    paired: List[Dict[str, Any]] = []
    for comment in review_comments:
        path = comment.get("path")
        file_entry = file_lookup.get(path, {})
        target_code = comment.get("diff_hunk")
        if not target_code:
            target_code = extract_code_for_line(file_entry.get("patch"), comment.get("line") or comment.get("original_line"))
        user = comment.get("user") or {}
        user_login = user.get("login") if isinstance(user, dict) else None
        paired.append(
            {
                "id": comment.get("id"),
                "user": user_login,
                "body": comment.get("body"),
                "path": path,
                "line": comment.get("line"),
                "original_line": comment.get("original_line"),
                "position": comment.get("position"),
                "file_url": file_entry.get("file_url"),
                "target_code": target_code,
            }
        )
    return paired


def main() -> int:
    args = parse_args()

    try:
        if args.repo_url:
            repo, pr_number = parse_repo_url(args.repo_url)
        else:
            if not args.repo or args.pr_number is None:
                raise ValueError("Provide either --repo-url or both repo and pr_number")
            repo = args.repo.strip()
            pr_number = args.pr_number
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if "/" not in repo:
        print("Repository must be in owner/repo format", file=sys.stderr)
        return 2

    try:
        base_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
        pr = make_request(base_url, token=args.token)
        files_payload = fetch_paginated_json(f"{base_url}/files", token=args.token)
        review_comments_payload = fetch_paginated_json(f"{base_url}/comments", token=args.token)
        issue_comments_payload = fetch_paginated_json(f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments", token=args.token)
    except GitHubRequestError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    changed_files: List[Dict[str, Any]] = []
    for index, file_item in enumerate(files_payload, start=1):
        patch = file_item.get("patch") or ""
        changed_files.append(
            {
                "path": file_item.get("filename"),
                "status": file_item.get("status"),
                "additions": file_item.get("additions"),
                "deletions": file_item.get("deletions"),
                "file_url": build_file_url(repo, pr_number, index),
                "patch": patch,
                "changed_code": extract_changed_code(patch),
                "review_comments": [],
            }
        )

    paired_comments = pair_comments_with_code(changed_files, review_comments_payload, repo, pr_number)
    for issue_comment in issue_comments_payload:
        paired_comments.append(
            {
                "id": issue_comment.get("id"),
                "user": (issue_comment.get("user") or {}).get("login"),
                "body": issue_comment.get("body"),
                "path": None,
                "line": None,
                "original_line": None,
                "position": None,
                "file_url": None,
                "target_code": "",
                "source": "issue_comment",
            }
        )
    for comment_entry in paired_comments:
        if comment_entry.get("path"):
            for file_entry in changed_files:
                if file_entry.get("path") == comment_entry.get("path"):
                    file_entry["review_comments"].append(
                        {
                            "id": comment_entry.get("id"),
                            "user": comment_entry.get("user"),
                            "body": comment_entry.get("body"),
                            "line": comment_entry.get("line"),
                            "original_line": comment_entry.get("original_line"),
                            "position": comment_entry.get("position"),
                            "target_code": comment_entry.get("target_code"),
                            "source": comment_entry.get("source", "review_comment"),
                        }
                    )
                    break
        else:
            changed_files[0]["review_comments"].append(
                {
                    "id": comment_entry.get("id"),
                    "user": comment_entry.get("user"),
                    "body": comment_entry.get("body"),
                    "line": comment_entry.get("line"),
                    "original_line": comment_entry.get("original_line"),
                    "position": comment_entry.get("position"),
                    "target_code": comment_entry.get("target_code"),
                    "source": comment_entry.get("source", "issue_comment"),
                }
            )

    result = {
        "repo": repo,
        "pull_request_id": pr_number,
        "title": pr.get("title"),
        "description": pr.get("body"),
        "changed_files": changed_files,
        "review_comments": paired_comments,
    }

    output_paths = []
    if args.output:
        output_paths.append(args.output)

    dataset_dir = os.path.join("dataset", args.report_name)
    dataset_file = os.path.join(dataset_dir, f"{pr_number}.json")
    output_paths.append(dataset_file)

    for output_path in output_paths:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
