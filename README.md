# Interview Prep Doc Agent

A background agent that watches a Google Doc and automatically formats interview prep notes using Claude. Drop raw notes into a tab — recruiter call, job description, technical concepts — and the agent rewrites it into a structured, highlighted prep document within one poll cycle.

---

## How it works

```
Google Doc (tabbed)
      │
      ▼
agent.py polls every 60s
      │  detects revision change
      ▼
Google Docs API  ──►  extract raw text from changed tab
      │
      ▼
Claude (claude-sonnet-4-6)
      │  applies mode-specific prompt
      │  (interview / technical / past_role)
      ▼
Formatted markdown output
      │
      ▼
Markdown parser  ──►  plain text + formatting instructions
      │                (headings → bold, bullets → list, ** → bold)
      ▼
Google Docs batchUpdate
      │  deleteContentRange → insertText → paragraph styles
      │  → text styles → bullet lists → highlights
      ▼
Updated tab in Google Doc + macOS notification
```

**Key design decisions:**
- Hash-based change detection — only re-formats tabs that actually changed, ignores the agent's own writes
- Single batchUpdate per tab — all formatting applied in one API call to avoid partial states
- Mode inference from parent tab name — same agent handles structurally different document types
- Prompts inject today's date — enables automatic status logic (In Progress / To Do / Feedback Received)

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```
Python 3.10+ recommended.

### 2. Google credentials
- Go to [Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials
- Create an OAuth 2.0 Client ID (Desktop app)
- Download as `credentials.json` and place it in this folder
- On first run, a browser window opens to authorize — `token.json` is saved automatically

### 3. Environment variables
```bash
export ANTHROPIC_API_KEY=your_key_here
export GOOGLE_DOC_ID=your_doc_id_here   # from the doc URL
```
Or copy `.env.example` to `.env` and fill in values.

### 4. Add your background
The "Tell Me About Yourself" section and the skill gap analysis are generated from your own background facts. Copy `background.example.md` to `background.md` and fill it in. `background.md` is gitignored, so your work history stays on your machine.

---

## Google Doc structure

The agent watches **child tabs** under these parent tabs:

| Parent tab | Mode | What it does |
|---|---|---|
| `Current Interviews` | `interview` | Metadata, tell me about yourself, skill gaps, prep checklist, questions |
| `Topics to Review` | `technical` | Q&A formatting, stub expansion, weak answer flagging |
| `Roles Applied To (PAST)` | `past_role` | Retrospectives: status, timeline, lessons learned, skill gaps |

Each child tab is processed independently.

---

## Running

### Format everything now (no polling)
```bash
python agent.py --now
```

### Format only one parent tab
```bash
python agent.py --now --only past        # Roles Applied To (PAST)
python agent.py --now --only interviews  # Current Interviews
python agent.py --now --only topics      # Topics to Review
```

### Run in background (polls every 60s, auto-formats on change)
```bash
python agent.py
```
Detach from terminal:
```bash
nohup python agent.py > agent.log 2>&1 &
```
- Check if running: `pgrep -f agent.py`
- Stop: `pkill -f agent.py`
- Watch logs: `tail -f agent.log`

### Cross-interview synthesis
```bash
python agent.py --synthesize              # prints to terminal
python agent.py --synthesize --tab TAB_ID # writes to a specific tab
```

### Inspect tab structure (find tab IDs)
```bash
python agent.py --inspect
```

### Voice practice mode
```bash
python webapp.py
```
Opens a local server at `http://127.0.0.1:5050`. Pick a "Topics to Review" sub-tab, and it quizzes you through every `Q:` in it out loud: reads the question via your browser's text-to-speech, records your spoken answer via your browser's speech recognition, and grades what you said against your own prep notes with Claude — verdict, gaps, and delivery feedback, spoken back to you.

Uses the browser's built-in Web Speech API (Chrome recommended) — no extra API keys beyond `ANTHROPIC_API_KEY`. Voice input isn't supported in every browser; if it's missing, you can still type your answer into the transcript box.

---

## What each mode produces

### Interview (`current interviews`)
1. Interview metadata — date, role, company, salary, status (auto-set)
2. Tell me about yourself — 3 paragraphs tailored to the JD
3. About the interviewer
4. Recruiter notes & focus areas
5. Role requirements breakdown (Must-Have / Nice-to-Have)
6. Skill gap analysis (⚠️ GAP / ❌ MISSING vs. your background)
7. Prep checklist — prioritized by likelihood
8. Feedback section
9. Questions to ask

**Follow-Up Status** is set automatically based on interview date and feedback:
- `In Progress` — upcoming, still prepping
- `To Do` — date passed, feedback not written yet
- `Feedback Received` — feedback filled in

### Technical (`topics to review`)
- Detects behavioral vs. technical content automatically
- Behavioral: applies STAR framework, flags missing components
- Technical: groups by topic, expands stub concepts into interview-ready explanations, flags weak answers with ⚠️ Review

### Past Role (`roles applied to (past)`)
1. Role metadata
2. Status + stage reached (Ghosted / Screened Out / Interviewed / etc.)
3. What happened — timeline
4. Skill gap analysis against the JD
5. Feedback & lessons learned
6. What I'd do differently
7. Full role details / JD preserved

---

## Highlights
Applied automatically on every write:
- **Yellow** — `[TODO — ...]` fields that need filling in
- **Orange** — `⚠️ Review:` flags on weak answers
- **Red** — `⚠️ GAP:` and `❌ MISSING:` skill gap flags

---

## Notifications
macOS notifications fire on every successful update and on errors. No extra setup needed — uses built-in `osascript`.
