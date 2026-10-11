"""Validate PR descriptions as data; never execute contributor-supplied text."""
import argparse
import json
import re
from pathlib import Path

SECTIONS = (
    "Need", "Change", "Acceptance",
    "Evidence", "Risk and rollback",
    "AI and review", "Documentation",
)


def validate_body(body, allow_template=False):
    if not isinstance(body, str):
        return ["PR body must be text"]
    text = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    # Fenced examples must not impersonate actual PR sections.
    masked = re.sub(r"^(`{3,}|~{3,}).*?^\1\s*$", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.M | re.S)
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", masked, re.M))
    errors = []
    for title in SECTIONS:
        matches = [i for i, item in enumerate(headings) if item.group(1) == title]
        if len(matches) != 1:
            errors.append(f"Expected exactly one section: {title}")
            continue
        i = matches[0]
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        content = text[headings[i].end():end].strip()
        value = re.sub(r"[\s*_`\[\]\-.:]+", " ", content).strip().lower()
        if not allow_template and (len(value) < 12 or re.match(r"^(todo|tbd|fill this in)\b", value) or value in {"n/a", "not applicable", "none", "pending"}):
            errors.append(f"Fill section with specific evidence or a reason: {title}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--body-file", type=Path)
    source.add_argument("--event", type=Path)
    parser.add_argument("--allow-template", action="store_true")
    args = parser.parse_args()
    if args.event:
        if args.allow_template:
            parser.error("--allow-template is forbidden for real PR events")
        event = json.loads(args.event.read_text())
        if "pull_request" not in event:
            parser.error("Expected a pull_request event")
        body = event["pull_request"].get("body")
    else:
        body = args.body_file.read_text()
    errors = validate_body(body, args.allow_template)
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        raise SystemExit(1)
    print("PASS: PR section structure" + (" (blank template only)" if args.allow_template else " and populated values; truth/authority require review"))


if __name__ == "__main__":
    main()
