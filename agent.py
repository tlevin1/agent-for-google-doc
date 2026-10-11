import re
import time
import os
from pathlib import Path
import sys
import subprocess
import hashlib
import datetime
import anthropic
import socket
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/documents"]
DOC_ID = os.environ.get("GOOGLE_DOC_ID")
POLL_INTERVAL = 60  # seconds

PARENT_MODE_MAP = {
    "current interviews": "interview",
    "topics to review": "technical",
    "roles applied to (past)": "past_role",
}

# Highlight colors
HIGHLIGHT_YELLOW = {"color": {"rgbColor": {"red": 1.0, "green": 0.949, "blue": 0.4}}}   # TODO fields
HIGHLIGHT_ORANGE = {"color": {"rgbColor": {"red": 1.0, "green": 0.737, "blue": 0.267}}} # Review flags
HIGHLIGHT_RED    = {"color": {"rgbColor": {"red": 1.0, "green": 0.6,   "blue": 0.6}}}   # Skill gaps


BACKGROUND_FILE = Path(__file__).with_name("background.md")


def load_background() -> str:
    """Your own background facts, kept out of the repo (background.md is gitignored)."""
    try:
        return BACKGROUND_FILE.read_text().strip()
    except FileNotFoundError:
        return "- (no background.md found; copy background.example.md and fill it in)"


def require_doc_id() -> None:
    if not DOC_ID:
        sys.exit("GOOGLE_DOC_ID is not set. Export it with the ID from your doc's URL (see .env.example).")


def notify(title: str, message: str) -> None:
    def _safe(s: str) -> str:
        return s.replace('"', "'").replace("\\", "").replace("\n", " ")[:100]
    subprocess.run(
        ["osascript", "-e", f'display notification "{_safe(message)}" with title "{_safe(title)}"'],
        check=False,
    )


def infer_mode(parent_name: str) -> str:
    return PARENT_MODE_MAP.get(parent_name.strip().lower(), "technical")


# ─────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────
INTERVIEW_PROMPT = """You are formatting a software engineering interview prep document that contains both logistics/recruiter notes and role-specific prep content.
Today's date: {today}

## Section 1 — Interview Metadata
Produce this table first, always. For any field not found in the input, write [TODO — fill in] as the value. Never leave a value cell blank.
| Field | Value |
|---|---|
| **Interview Date** | |
| **Role** | |
| **Company** | |
| **Salary Range** | |
| **Location** | |
| **Recruiter** | |
| **Interviewer** | |
| **Role Link** | |
| **Follow-Up Status** | |

Follow-Up Status rules (apply in order):
1. If the input contains actual post-interview feedback (not a TODO placeholder) → "Feedback Received"
2. If the interview date is known and has already passed (before today's date) → "To Do" (follow-up needed)
3. Otherwise → "In Progress"

## Section 2 — Tell Me About Yourself
Output exactly 3 paragraphs of plain spoken text. Do NOT use quotation marks. Do NOT merge into one paragraph. Do NOT add any label or preamble before the text.

Background facts (do not invent anything beyond these):
<<BACKGROUND>>

Paragraph structure — output each as its own paragraph with NO blank lines between them:
**Paragraph 1:** **[Bold this entire first paragraph.]** Who you are and your career arc — 2 sentences max.
**Paragraph 2:** What you build now — lead with the skills most relevant to this specific JD. If no JD is present, lead with agentic workflows and RAG as the default.
**Paragraph 3:** Why this role at this company specifically — use the actual company name, tie it to the JD requirements. If no JD or company name is present, write: [TODO — add company-specific closing paragraph once JD is available]

Keep it under 90 seconds when read aloud. Natural spoken tone, no em-dash chains or run-on sentences.

## Section 3 — About the Interviewer
Summarize their background in 3–5 bullets. Flag anything useful for tailoring answers (e.g. cloud background → weight AWS answers; manufacturing background → use operational/process examples).
If no interviewer info present, omit this section entirely.

## Section 4 — Recruiter Notes
Clean up and organize any recruiter call notes, logistics, or context. Preserve all wording exactly.
If none present, omit.

## Section 5 — Recruiter Focus Areas
Bulleted list of exactly what the recruiter flagged to study. These are highest priority.
If none flagged, omit.

## Section 6 — Role Requirements Breakdown
**Must-Have** — required qualifications only
**Nice-to-Have** — preferred qualifications only
If no JD is present, write: [TODO — paste JD requirements]

## Section 7 — Skill Gap Analysis
Compare the role requirements against the background facts provided. For each requirement:
- If clearly covered by the background → skip
- If partially covered or uncertain → flag as: ⚠️ GAP: [requirement] — [one line on what to prepare]
- If not covered at all → flag as: ❌ MISSING: [requirement] — [one line on what to prepare or whether to be upfront]
If no JD is present, write: [TODO — complete once JD requirements are filled in]

## Section 8 — Prep Checklist
Prioritized checklist based on recruiter focus + JD + skill gaps. Format:
- [ ] Topic — why it matters (one line)
Order by likelihood of coming up. Items from skill gaps should appear at the top.
If no JD or recruiter focus present, write: [TODO — complete once JD and recruiter notes are added]

## Section 9 — Feedback
If post-interview feedback is present in the input, format it here. Include: what went well, what missed, specific questions fumbled, and what to do differently.
Otherwise insert: [TODO — add post-interview impressions]

## Section 10 — Questions to Ask
4–6 specific questions for this interviewer tied to their background and the role. Not generic. If interviewer background is unknown, write questions tied to the role and company only.

## Global rules:
- **Never remove anything.** Every word, name, date, link, note, question, and answer from the input must appear in the output. If you are unsure whether to include something, include it exactly as written.
- Only bold text explicitly marked with ** in the input, section headers, table field labels, and question labels (Q:) as specified above. Never bold regular body text, answer content, or TODO placeholders.
- Only include sections where content exists or a TODO placeholder makes sense.
- Format all tables with consistent columns. Every row must have the same number of cells. Never leave a cell empty — use [TODO — fill in] if value is missing.
- Return only formatted text, no preamble."""


TECHNICAL_PROMPT = """You are formatting a software engineering interview prep document.

For each question or item in the input, classify it independently:
- **Behavioral**: experience-based or situational Q&A ("Tell me about a time...", "were you involved in...")
- **Technical**: coding problems, system design, concept explanations, or domain Q&A (DS, ML, AI, Python, etc.)

Documents can freely mix both types — classify item by item, not the document as a whole, and apply the matching treatment below to each.

## If Behavioral:
- Apply STAR to every answer: **Situation:** / **Task:** / **Action:** / **Result:**
- If a component is missing: **[label]:** [TODO — add detail]
- Group related questions under ## subheaders
- After each answer, evaluate quality. Flag if: missing Result with metrics, Action is vague, or Situation has no real stakes.
  Add: ⚠️ Review: [specific reason]
- Format every question as: **Q: [question text]**
- Answer text (Situation/Task/Action/Result labels aside) stays unbolded

## If Technical:
- Do NOT apply STAR
- Structure each answer: Approach → Key Decisions → Trade-offs → Edge Cases (only include sections that have content)
- Group in two levels:
  - Broad category as a ## header (e.g., ## Vector Search, ## RAG, ## Model Training) — the general area a domain expert would recognize
  - Narrow topic as a ### subheader nested under it (e.g., ### Vector Databases, ### Vector Similarity, ### Dense vs. Sparse Vectors all under ## Vector Search)
  - Every narrow topic must sit under exactly one broad category. If a topic doesn't obviously belong to any other topic's category, give it its own ## category (don't force unrelated things together just to avoid a singleton).
  - Pull matching narrow topics together under the same ## category even if they appear scattered far apart in the input.
- Preserve all code snippets, variable names, and terminology exactly as written
- Format all code blocks with``` markers and consistent indentation
- Format every question or prompt as: **Q: [question text]**
- If the input is a concept/topic rather than a literal question (e.g. "RAG vs fine-tuning"), still prefix it: **Q: What is [topic]? / How does [topic] work?** — infer a natural question phrasing from the topic name
- Answer/explanation text stays unbolded

### Stub detection — apply to every concept, term, or topic found in the input:
A "stub" is any concept that appears in the notes but has no real explanation — just a name, a one-line mention, or a parenthetical. For every stub:
1. Give it its own ### subheader, nested under the appropriate ## broad category (same two-level grouping rule as above)
2. Copy whatever was written (even if just the name or parenthetical) as-is, on its own line labeled **Your notes:**
3. Below that, add **Expanded:** and write a concise interview-ready explanation: what it is, how it works, why it matters, one key trade-off or use case
4. Keep the expansion tight — 3–6 sentences, practical, not academic
5. Never overwrite or replace the original notes — always preserve them above your expansion

For concepts that already have a full explanation in the notes, preserve them as-is under **Your notes:** and do not add an expansion.

### Answer quality review:
After formatting each Q&A or concept explanation, evaluate whether the answer is strong enough for an interview. Flag it if any of the following are true:
- Only defines the term with no "how it works" or "why it matters"
- No concrete example or real use case
- Missing trade-offs when the topic clearly has them (e.g. RAG vs fine-tuning, SQL vs NoSQL)
- Too vague to actually say out loud in an interview ("it helps with performance", "it's used in AI")
- Answer is one sentence for a topic that warrants a paragraph

When flagging, add this line directly under the answer:
⚠️ Review: [one specific reason — e.g. "no example", "missing trade-offs", "too vague to say in an interview"]

Do NOT flag:
- Answers that are complete and specific even if short
- Stubs you already expanded (you wrote those, they're fine)
- Questions that are clearly notes-to-self, not answers

## Global rules:
- **Never remove anything.** Every word, question, answer, code snippet, and note from the input must appear in the output. Preserve original notes exactly — expansions go below them, never replace them.
- Only bold text explicitly marked ** in the input, section headers, and STAR labels. Never bold regular answer text.
- Return only formatted text, no preamble."""


PAST_ROLE_PROMPT = """You are formatting a job application retrospective document.

## Section 1 — Role Metadata
Produce this block first, always. For any field not found in the input, write [TODO — fill in]. Never leave a value blank.
**Company:**
**Role:**
**Date Applied:**
**Salary Range:**
**Location:**
**Source:** (where the role was found — LinkedIn, referral, recruiter, etc.)
**Recruiter/Contact:**

## Section 2 — Status
Set status to exactly one of these based on what happened:
- Ghosted — applied, no response
- Screened Out — rejected before any interview
- Interviewed — got interviews but didn't progress past a certain round
- Offer Declined — received an offer but turned it down
- Offer Accepted — accepted
- Withdrew — pulled out of the process

Format as:
**Status:** [value]
**Stage Reached:** [e.g. "applied", "recruiter screen", "technical round 1", "final round", "offer"]

If the input contains enough info to determine these, fill them in. If not, write [TODO — fill in].

## Section 3 — What Happened
A short factual timeline of what occurred — recruiter outreach, interview rounds, any feedback received, how it ended. Preserve all wording from the input exactly. If sparse, write what's there and add [TODO — fill in timeline].

## Section 4 — Skill Gap Analysis
Compare what the role required against this candidate's background:
<<BACKGROUND>>

For each major requirement in the JD:
- If clearly covered → skip
- If partially covered → ⚠️ GAP: [requirement] — [what to address]
- If not covered → ❌ MISSING: [requirement] — [whether to prep or be upfront]

If no JD is present, write: [TODO — complete once role details are added]

## Section 5 — Feedback & Lessons Learned
Preserve everything the user wrote here exactly as written. Clean up formatting only — do not reword or summarize.
If nothing is written, insert: [TODO — what went well, what could have gone better, what to do differently next time]

## Section 6 — What I'd Do Differently
Preserve any notes here exactly. If empty, insert: [TODO — specific actions to take differently for a similar role]

## Section 7 — Role Details
Preserve all job description content, requirements, and role info from the input exactly as written. Format it as:
**Must-Have** — required qualifications only
**Nice-to-Have** — preferred qualifications only
If there is other role info (team description, responsibilities, compensation details, etc.) include it under appropriate subheaders. Do not summarize or shorten any of it.

## Global rules:
- **Never remove anything.** Every word and note from the input must appear in the output.
- Only bold field labels and section headers. Never bold regular body text or TODO placeholders.
- Format all tables with consistent columns. Never leave a cell empty.
- Return only formatted text, no preamble."""


SYNTHESIS_PROMPT = """You are analyzing a collection of software engineering interview notes to identify patterns and improvement areas across all interviews.

Today's date: {today}

You will receive the combined content of multiple interview tabs. Your job is to produce a cross-interview synthesis document.

## Section 1 — Interview Pipeline Summary
A table summarizing all interviews found:
| Company | Role | Date | Status | Stage Reached |
|---|---|---|---|---|

## Section 2 — Recurring Weak Spots
Topics or question types that appear across multiple interviews where the answers were flagged as weak, vague, or incomplete. For each:
- **Topic:** [name]
- **Appeared in:** [companies/interviews]
- **Pattern:** [what specifically keeps going wrong — e.g. "gives thin examples", "can't justify model selection", "no metrics in results"]
- **Fix:** [one concrete action — e.g. "prepare 2 stories with specific metrics", "memorize latency/cost tradeoffs for GPT vs Claude vs Llama"]

## Section 3 — Consistent Strengths
What is landing well across interviews. Be specific — not "good communication" but "client-facing problem scoping stories resonate well with interviewers from consulting backgrounds."

## Section 4 — Skill Gaps Appearing Across Roles
Requirements that keep showing up in JDs that aren't well covered by current experience. Rank by frequency.

## Section 5 — Priority Actions
Top 5 things to work on right now, ordered by impact on getting offers. Each should be specific and actionable — not "study ML" but "build one end-to-end project that goes from training to deployed API, so you have a post-deployment story."

## Global rules:
- Be direct and critical. This is for self-improvement, not encouragement.
- Only bold section headers and field labels.
- Return only formatted text, no preamble."""


ACCOMPLISHMENTS_PROMPT = """You are formatting an interview accomplishments document.
The document contains Q&A pairs and notes about multiple past projects, mixing behavioral and technical content.

Your job is to reorganize everything by **project**, so all questions and notes related to the same project are grouped together.

## Structure

Use this two-level hierarchy:
- `##` header = project name (e.g., "## HR Policy RAG Assistant", "## ERP Integration Pilot")
- `###` header = specific question or topic within that project (e.g., "### Walk Me Through the Project", "### Prompting Techniques Used")

If a topic or question clearly applies to a specific project, nest it under that project.
If a topic is genuinely cross-project (e.g., a general technique used across several projects), put it under a `## Cross-Project Patterns` section at the end.

Infer project names from context if not explicitly stated — use descriptive names like "## HR Policy RAG Assistant" rather than just "## RAG".

## Per-item treatment

For each Q&A or note, classify it as behavioral or technical and apply the matching treatment:

**If behavioral** (experience-based, "tell me about a time", "walk me through", "were you involved in"):
- Apply STAR: **Situation:** / **Task:** / **Action:** / **Result:**
- If a component is missing: **[label]:** [TODO — add detail]
- After formatting, evaluate quality. Flag if: Result has no metrics, Action is vague, Situation has no real stakes.
  Add: ⚠️ Review: [specific reason]

**If technical** (concepts, architecture decisions, tradeoffs, how-it-works):
- Structure: Approach → Key Decisions → Trade-offs → Edge Cases (only sections with content)
- Preserve all code snippets, variable names, and terminology exactly
- Format code blocks with ``` markers

Format every question as: **Q: [question text]**

### Stub detection
A stub is any concept or topic with no real explanation — just a name, one-line mention, or parenthetical. For every stub:
1. Place it under the right project's `###` subheader
2. Preserve whatever was written as-is under **Your notes:**
3. Add **Expanded:** with a concise interview-ready explanation: what it is, how it works, why it matters, one key trade-off or use case (3–6 sentences)
4. Never overwrite original notes

For concepts with a full explanation already, preserve them under **Your notes:** with no expansion.

### Answer quality review
Flag an answer if:
- Only defines the term with no "how it works" or "why it matters"
- No concrete example or real use case
- Missing trade-offs when the topic clearly has them
- Too vague to say in an interview
- One sentence for a topic that warrants a paragraph

Add: ⚠️ Review: [one specific reason]

Do NOT flag: complete and specific answers, stubs you already expanded, notes-to-self.

## Global rules
- **Never remove anything.** Every word, question, answer, code snippet, and note must appear in the output. Preserve original notes exactly — expansions go below, never replace them.
- Only bold text explicitly marked ** in the input, section headers, and STAR labels. Never bold regular answer text.
- Return only formatted text, no preamble."""

PROMPT_MAP = {
    "interview": INTERVIEW_PROMPT,
    "technical": TECHNICAL_PROMPT,
    "past_role": PAST_ROLE_PROMPT,
    "synthesis": SYNTHESIS_PROMPT,
    "accomplishments": ACCOMPLISHMENTS_PROMPT,
}


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────
def get_creds() -> Credentials:
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return creds


# ─────────────────────────────────────────────
# GOOGLE DOCS HELPERS
# ─────────────────────────────────────────────
def get_child_tabs_of(service, doc_id: str, parent_tab_name: str) -> list[tuple[str, str]]:
    """Return list of (tab_id, title) for all direct children of a named parent tab."""
    doc = service.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    tabs = doc.get("tabs", [])

    def find_parent(tabs):
        for tab in tabs:
            props = tab.get("tabProperties", {})
            if props.get("title", "").strip().lower() == parent_tab_name.strip().lower():
                return tab
            result = find_parent(tab.get("childTabs", []))
            if result:
                return result
        return None

    parent = find_parent(tabs)
    if not parent:
        available = [t.get("tabProperties", {}).get("title") for t in tabs]
        raise ValueError(f"Tab '{parent_tab_name}' not found. Available: {available}")

    return [
        (c.get("tabProperties", {}).get("tabId"), c.get("tabProperties", {}).get("title"))
        for c in parent.get("childTabs", [])
    ]


def find_tab_in(tabs: list, tab_id: str) -> dict:
    """Recursively find a tab by ID from an already-fetched tabs list."""
    for tab in tabs:
        if tab.get("tabProperties", {}).get("tabId") == tab_id:
            return tab
        try:
            return find_tab_in(tab.get("childTabs", []), tab_id)
        except ValueError:
            continue
    raise ValueError(f"Tab ID {tab_id} not found")


def extract_text_from_tab(tab: dict) -> str:
    body = tab.get("documentTab", {}).get("body", {})
    text = []
    for block in body.get("content", []):
        para = block.get("paragraph")
        if not para:
            continue
        for elem in para.get("elements", []):
            t = elem.get("textRun", {}).get("content", "")
            text.append(t)
    return "".join(text)


def get_tab_end_index(tab: dict) -> int:
    body = tab.get("documentTab", {}).get("body", {})
    content = body.get("content", [])
    return content[-1]["endIndex"] - 1 if content else 1


# ─────────────────────────────────────────────
# CLAUDE
# ─────────────────────────────────────────────
def format_with_claude(raw_text: str, mode: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    for attempt in range(3):
        try:
            today = datetime.date.today().strftime("%Y-%m-%d")
            prompt_template = PROMPT_MAP[mode]
            system_prompt = (
                prompt_template.format(today=today)
                if "{today}" in prompt_template
                else prompt_template
            ).replace("<<BACKGROUND>>", load_background())
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=128000,
                system=system_prompt,
                messages=[{"role": "user", "content": f"Format these interview notes:\n\n{raw_text}"}],
            ) as stream:
                message = stream.get_final_message()
            return message.content[0].text

        except anthropic.BadRequestError as e:
            raise RuntimeError(
                f"Input too long for Claude ({len(raw_text):,} chars). "
                f"Trim the tab content and try again. Detail: {e}"
            ) from e

        except anthropic.RateLimitError as e:
            if attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"  Rate limited. Retrying in {wait}s... (attempt {attempt + 1}/3)")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Rate limit hit after 3 attempts: {e}") from e

        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"  Claude overloaded. Retrying in {wait}s... (attempt {attempt + 1}/3)")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Claude API error {e.status_code}: {e}") from e

    raise RuntimeError("format_with_claude exhausted retries")


def synthesize_all_interviews(service, doc_id: str) -> str:
    """Pull content from all interview child tabs and run synthesis prompt."""
    print("  Gathering all interview tabs for synthesis...")
    try:
        children = get_child_tabs_of(service, doc_id, "current interviews")
    except ValueError as e:
        raise RuntimeError(f"Could not find interview tabs: {e}") from e

    doc_full = service.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    combined = []
    for tab_id, title in children:
        try:
            tab = find_tab_in(doc_full.get("tabs", []), tab_id)
            text = extract_text_from_tab(tab)
            combined.append(f"=== {title} ===\n{text}")
        except ValueError:
            continue

    if not combined:
        raise RuntimeError("No interview tab content found for synthesis.")

    return format_with_claude("\n\n".join(combined), "synthesis")


# ─────────────────────────────────────────────
# DOC WRITE
# ─────────────────────────────────────────────
def _strip_bold(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return (plain_text, [(start, end), ...]) with 0-indexed bold ranges."""
    plain, bolds = [], []
    i = cursor = 0
    while i < len(text):
        if text[i:i + 2] == "**":
            end = text.find("**", i + 2)
            if end != -1:
                content = text[i + 2:end]
                bolds.append((cursor, cursor + len(content)))
                plain.append(content)
                cursor += len(content)
                i = end + 2
                continue
        plain.append(text[i])
        cursor += 1
        i += 1
    return "".join(plain), bolds


def _parse_lines(formatted_text: str) -> list[tuple[str, str, list]]:
    """
    Parse markdown into [(plain_text, line_type, bold_ranges), ...].
    line_type: 'h1' | 'h2' | 'h3' | 'bullet' | 'numbered' | 'code' | 'normal' | 'sep'
    """
    parsed = []
    in_code_block = False

    for raw in formatted_text.split("\n"):
        s = raw.rstrip()

        # Code block toggle
        if s.startswith("```"):
            in_code_block = not in_code_block
            parsed.append(("", "sep", []))
            continue

        if in_code_block:
            parsed.append((s, "code", []))
            continue

        if s.startswith("### "):
            txt, bolds = _strip_bold(s[4:])
            parsed.append((txt, "h3", bolds))
        elif s.startswith("## "):
            txt, bolds = _strip_bold(s[3:])
            parsed.append((txt, "h2", bolds))
        elif s.startswith("# "):
            txt, bolds = _strip_bold(s[2:])
            parsed.append((txt, "h1", bolds))
        elif s in ("---", "***", "___"):
            parsed.append(("", "sep", []))
        elif re.match(r"^- \[[x ]\] ", s):
            txt, bolds = _strip_bold(s[6:])
            parsed.append((txt, "bullet", bolds))
        elif s.startswith("- ") or s.startswith("* "):
            txt, bolds = _strip_bold(s[2:])
            parsed.append((txt, "bullet", bolds))
        elif re.match(r"^\d+\. ", s):
            m = re.match(r"^\d+\. ", s)
            txt, bolds = _strip_bold(s[m.end():])
            parsed.append((txt, "numbered", bolds))
        elif s.startswith("> "):
            txt, bolds = _strip_bold(s[2:])
            parsed.append((txt, "normal", bolds))
        elif s.startswith("|") and s.endswith("|"):
            inner = s.replace("|", "").replace("-", "").replace(" ", "").replace(":", "")
            if not inner:
                parsed.append(("", "sep", []))
            else:
                cells = [c.strip() for c in s.split("|")[1:-1] if c.strip()]
                if len(cells) >= 2:
                    label = cells[0].replace("**", "")
                    val_raw = " ".join(cells[1:]).strip()
                    val, val_bolds = _strip_bold(val_raw)
                    if label.lower() in ("field", "column", "key") and val.lower() in ("value", "description"):
                        parsed.append(("", "sep", []))
                    elif val:
                        combined = f"{label}: {val}"
                        offset = len(label) + 2
                        bolds = [(0, len(label))] + [(b[0] + offset, b[1] + offset) for b in val_bolds]
                        parsed.append((combined, "normal", bolds))
                    else:
                        parsed.append((label, "normal", [(0, len(label))]))
                elif cells:
                    txt, bolds = _strip_bold(cells[0])
                    parsed.append((txt, "normal", bolds))
                else:
                    parsed.append(("", "sep", []))
        else:
            txt, bolds = _strip_bold(s)
            parsed.append((txt, "normal", bolds))

    return parsed


def _utf16_offsets(text: str) -> list[int]:
    """offsets[i] = position of text[i] in UTF-16 code units, which is how Google
    Docs indexes text. Characters outside the Basic Multilingual Plane (most
    emoji) take two units, so Python string positions drift after each one."""
    offsets = [0]
    for ch in text:
        offsets.append(offsets[-1] + (2 if ord(ch) > 0xFFFF else 1))
    return offsets


def build_write_requests(formatted_text: str, tab_id: str, end_index: int) -> list:
    requests = []

    # 1. Clear existing content
    if end_index > 1:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end_index, "tabId": tab_id}
            }
        })

    # 2. Parse markdown, collapse blank lines
    parsed = _parse_lines(formatted_text)
    collapsed = []
    for entry in parsed:
        is_blank = entry[0] == ""
        prev_is_blank = collapsed and collapsed[-1][0] == ""
        prev_is_heading = collapsed and collapsed[-1][1] in ("h1", "h2", "h3")
        if is_blank and (prev_is_blank or prev_is_heading):
            continue
        collapsed.append(entry)
    parsed = collapsed

    plain_text = "\n".join(p[0] for p in parsed)
    if not plain_text.endswith("\n"):
        plain_text += "\n"

    requests.append({
        "insertText": {
            "location": {"index": 1, "tabId": tab_id},
            "text": plain_text,
        }
    })

    # Every Docs index below goes through this map: 1 + offsets[python_position].
    offsets = _utf16_offsets(plain_text)
    doc_len = offsets[-1]

    # 3a. Reset paragraph style
    requests.append({
        "updateParagraphStyle": {
            "range": {"startIndex": 1, "endIndex": doc_len, "tabId": tab_id},
            "paragraphStyle": {
                "spaceAbove": {"magnitude": 0, "unit": "PT"},
                "spaceBelow": {"magnitude": 0, "unit": "PT"},
                "lineSpacing": 115,
                "alignment": "START",
            },
            "fields": "spaceAbove,spaceBelow,lineSpacing,alignment",
        }
    })

    # 3b. Reset text style
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": 1, "endIndex": doc_len, "tabId": tab_id},
            "textStyle": {
                "bold": False,
                "italic": False,
                "underline": False,
                "backgroundColor": {},
                "foregroundColor": {},
                "fontSize": {"magnitude": 11, "unit": "PT"},
                "weightedFontFamily": {"fontFamily": "Arial", "weight": 400},
            },
            "fields": "bold,italic,underline,backgroundColor,foregroundColor,fontSize,weightedFontFamily",
        }
    })

    # 4. Apply per-line formatting
    cursor = 0  # position in plain_text (code points)

    for plain_line, ltype, bolds in parsed:
        line_len = len(plain_line)
        line_start = 1 + offsets[cursor]
        line_end = 1 + offsets[cursor + line_len]

        if line_len > 0:
            if ltype in ("h1", "h2", "h3"):
                # Distinct sizes so broad (##) and narrow (###) headers are visually
                # distinguishable in the doc, not just bold like everything else.
                heading_size = {"h1": 16, "h2": 13, "h3": 11}[ltype]
                requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": line_start, "endIndex": line_end, "tabId": tab_id},
                        "textStyle": {
                            "bold": True,
                            "fontSize": {"magnitude": heading_size, "unit": "PT"},
                        },
                        "fields": "bold,fontSize",
                    }
                })
            elif ltype == "code":
                requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": line_start, "endIndex": line_end, "tabId": tab_id},
                        "textStyle": {
                            "weightedFontFamily": {"fontFamily": "Courier New", "weight": 400},
                            "fontSize": {"magnitude": 10, "unit": "PT"},
                            "backgroundColor": {"color": {"rgbColor": {"red": 0.95, "green": 0.95, "blue": 0.95}}},
                        },
                        "fields": "weightedFontFamily,fontSize,backgroundColor",
                    }
                })
            elif ltype in ("bullet", "numbered"):
                requests.append({
                    "createParagraphBullets": {
                        "range": {"startIndex": line_start, "endIndex": line_end + 1, "tabId": tab_id},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                })

        for b_start, b_end in bolds:
            abs_start = 1 + offsets[cursor + b_start]
            abs_end = 1 + offsets[cursor + b_end]
            if abs_end > abs_start:
                requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": abs_start, "endIndex": abs_end, "tabId": tab_id},
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                })

        cursor += line_len + 1

    # 5. Highlight TODOs (yellow)
    for match in re.finditer(r'\[TODO[^\]]*\]', plain_text):
        start = 1 + offsets[match.start()]
        end = 1 + offsets[match.end()]
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end, "tabId": tab_id},
                "textStyle": {"backgroundColor": HIGHLIGHT_YELLOW},
                "fields": "backgroundColor",
            }
        })

    # 6. Highlight review flags (orange)
    for match in re.finditer(r'⚠️ Review:.*', plain_text):
        start = 1 + offsets[match.start()]
        end = 1 + offsets[match.end()]
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end, "tabId": tab_id},
                "textStyle": {"backgroundColor": HIGHLIGHT_ORANGE},
                "fields": "backgroundColor",
            }
        })

    # 7. Highlight skill gaps (red)
    for match in re.finditer(r'(⚠️ GAP:|❌ MISSING:).*', plain_text):
        start = 1 + offsets[match.start()]
        end = 1 + offsets[match.end()]
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end, "tabId": tab_id},
                "textStyle": {"backgroundColor": HIGHLIGHT_RED},
                "fields": "backgroundColor",
            }
        })

    return requests


def batch_update(service, doc_id: str, requests: list, chunk_size: int = 100) -> None:
    """Send batchUpdate requests in chunks.

    The first four requests are always structural (delete, insert, reset-paragraph,
    reset-text). The insertText request can be very large, so we send each of the
    first four individually to avoid oversized HTTP bodies. The remaining
    per-line formatting requests are sent in batches of chunk_size.
    """
    structural = requests[:4]
    formatting = requests[4:]
    for req in structural:
        service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [req]},
        ).execute()
    for i in range(0, len(formatting), chunk_size):
        service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": formatting[i:i + chunk_size]},
        ).execute()


# ─────────────────────────────────────────────
# ONE-SHOT
# ─────────────────────────────────────────────
def run_once(only: str = None, child: str = None):
    """Format every child tab under all watched parent tabs immediately.
    If `child` is given, only the matching child tab(s) (by title substring) are processed."""
    socket.setdefaulttimeout(30)
    creds = get_creds()
    service = build("docs", "v1", credentials=creds)

    for parent_name in PARENT_MODE_MAP:
        if only and only.lower() not in parent_name.lower():
            print(f"Skipping '{parent_name}' (--only filter)")
            continue
        print(f"Processing children of '{parent_name}'...")
        try:
            children = get_child_tabs_of(service, DOC_ID, parent_name)
        except ValueError as e:
            print(f"  WARNING: {e}")
            continue

        if child:
            children = [(tab_id, title) for tab_id, title in children if child.lower() in title.lower()]
            if not children:
                print(f"  No child tab matching '{child}' under '{parent_name}'")
                continue

        doc_full = service.documents().get(documentId=DOC_ID, includeTabsContent=True).execute()

        for tab_id, title in children:
            mode = infer_mode(parent_name)
            print(f"  Formatting '{title}' (mode: {mode})...")
            try:
                tab = find_tab_in(doc_full.get("tabs", []), tab_id)
                raw_text = extract_text_from_tab(tab)
                end_index = get_tab_end_index(tab)
                formatted = format_with_claude(raw_text, mode)
                requests = build_write_requests(formatted, tab_id, end_index)
                batch_update(service, DOC_ID, requests)
                plain_text = "\n".join(p[0] for p in _parse_lines(formatted))
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"  ✓ [{ts}] '{title}' updated ({len(plain_text):,} chars written)")
                notify("Doc Agent", f"✓ '{title}' updated")
            except Exception as e:
                print(f"  ERROR on '{title}': {e}")
                notify("Doc Agent", f"❌ Error on '{title}': {e}")


def run_synthesis(target_tab_id: str = None):
    """Run cross-interview synthesis and write to a target tab.
    If target_tab_id is None, prints the output instead."""
    socket.setdefaulttimeout(30)
    creds = get_creds()
    service = build("docs", "v1", credentials=creds)

    formatted = synthesize_all_interviews(service, DOC_ID)

    if target_tab_id:
        doc_full = service.documents().get(documentId=DOC_ID, includeTabsContent=True).execute()
        tab = find_tab_in(doc_full.get("tabs", []), target_tab_id)
        end_index = get_tab_end_index(tab)
        requests = build_write_requests(formatted, target_tab_id, end_index)
        batch_update(service, DOC_ID, requests)
        print(f"✓ Synthesis written to tab {target_tab_id}")
        notify("Doc Agent", "✓ Synthesis complete")
    else:
        print("\n=== SYNTHESIS OUTPUT (no tab ID provided, printing only) ===\n")
        print(formatted)


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def main():
    socket.setdefaulttimeout(30)
    creds = get_creds()
    service = build("docs", "v1", credentials=creds)

    watched_tabs: dict[str, dict] = {}
    for parent_name in PARENT_MODE_MAP:
        print(f"Looking for children of '{parent_name}'...")
        try:
            children = get_child_tabs_of(service, DOC_ID, parent_name)
        except ValueError as e:
            print(f"  WARNING: {e}")
            continue

        for tab_id, title in children:
            print(f"  Watching: '{title}' (ID: {tab_id}, mode: {infer_mode(parent_name)})")
            watched_tabs[tab_id] = {
                "name": title,
                "parent": parent_name,
                "last_hash": None,
            }

    if not watched_tabs:
        print("No child tabs found to watch. Exiting.")
        return

    print(f"\nWatching {len(watched_tabs)} tab(s). Poll interval: {POLL_INTERVAL}s\n")
    last_revision = None

    while True:
        try:
            doc = service.documents().get(documentId=DOC_ID).execute()
            current_revision = doc["revisionId"]

            if last_revision is None:
                last_revision = current_revision
                doc_full = service.documents().get(
                    documentId=DOC_ID, includeTabsContent=True
                ).execute()
                for tab_id, info in watched_tabs.items():
                    try:
                        tab = find_tab_in(doc_full.get("tabs", []), tab_id)
                        raw_text = extract_text_from_tab(tab)
                        info["last_hash"] = hashlib.md5(raw_text.encode()).hexdigest()
                    except ValueError as e:
                        print(f"  WARNING: {e}")
                print("Initial state captured. Watching for changes...\n")

            elif current_revision != last_revision:
                print(f"Change detected (revision {current_revision}). Checking tabs...")

                doc_full = service.documents().get(
                    documentId=DOC_ID, includeTabsContent=True
                ).execute()

                for tab_id, info in watched_tabs.items():
                    try:
                        tab = find_tab_in(doc_full.get("tabs", []), tab_id)
                    except ValueError as e:
                        print(f"  WARNING: {e}")
                        continue

                    raw_text = extract_text_from_tab(tab)
                    end_index = get_tab_end_index(tab)
                    content_hash = hashlib.md5(raw_text.encode()).hexdigest()

                    if content_hash == info["last_hash"]:
                        continue

                    print(f"  Formatting '{info['name']}' (mode: {infer_mode(info['parent'])})...")

                    try:
                        mode = infer_mode(info["parent"])
                        formatted = format_with_claude(raw_text, mode)
                        requests = build_write_requests(formatted, tab_id, end_index)
                        batch_update(service, DOC_ID, requests)

                        plain_text = "\n".join(p[0] for p in _parse_lines(formatted))
                        info["last_hash"] = hashlib.md5(plain_text.encode()).hexdigest()
                        ts = datetime.datetime.now().strftime("%H:%M:%S")
                        print(
                            f"  ✓ [{ts}] '{info['name']}' updated "
                            f"(parent: '{info['parent']}', mode: {mode}, "
                            f"{len(plain_text):,} chars written)"
                        )
                        notify("Doc Agent", f"✓ '{info['name']}' updated")

                    except Exception as e:
                        print(f"  ERROR on '{info['name']}': {e}")
                        notify("Doc Agent", f"❌ Error on '{info['name']}': {e}")

                last_revision = current_revision

        except Exception as e:
            print(f"POLL ERROR: {e}")

        time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    require_doc_id()
    if "--now" in sys.argv:
        only_arg = None
        if "--only" in sys.argv:
            idx = sys.argv.index("--only")
            only_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        child_arg = None
        if "--child" in sys.argv:
            idx = sys.argv.index("--child")
            child_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        run_once(only=only_arg, child=child_arg)

    elif "--synthesize" in sys.argv:
        tab_arg = None
        if "--tab" in sys.argv:
            idx = sys.argv.index("--tab")
            tab_arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        run_synthesis(target_tab_id=tab_arg)

    elif "--inspect" in sys.argv:
        creds = get_creds()
        service = build("docs", "v1", credentials=creds)
        doc = service.documents().get(documentId=DOC_ID, includeTabsContent=True).execute()

        def print_tabs(tabs, indent=0):
            for tab in tabs:
                props = tab.get("tabProperties", {})
                print(" " * indent + f"'{props.get('title')}' — ID: {props.get('tabId')}")
                print_tabs(tab.get("childTabs", []), indent + 2)

        print_tabs(doc.get("tabs", []))

    else:
        main()