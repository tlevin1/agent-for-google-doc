"""
Voice practice server. Reads Q&A pairs out of "Topics to Review" tabs and
runs a mock interview loop: the browser handles speech-to-text/text-to-speech
(Web Speech API), this server picks questions and grades spoken answers
against your own prep notes via Claude.
"""
import os

import anthropic
from flask import Flask, jsonify, request, send_from_directory
from googleapiclient.discovery import build

from agent import DOC_ID, find_tab_in, get_child_tabs_of, get_creds, require_doc_id

PARENT_TAB_NAME = "topics to review"

FEEDBACK_PROMPT = """You are a strict but encouraging interview coach running a live mock interview.

You'll get an interview question, the candidate's own prep notes for it (their prepared answer),
and a transcript of what they just said out loud (transcribed from speech — minor transcription
glitches like dropped words or misheard homophones are not the candidate's fault, don't penalize those).

Evaluate the spoken answer:
1. Does it cover the key points from their prep notes? Call out anything important that's missing.
2. Is anything stated incorrectly or confused?
3. If the transcript is rambling, has no clear structure, or is way too short to say in a real interview, say so.
4. Give 2-4 sentences of specific, actionable feedback — what to keep, what to add, what to cut.
5. End with a one-line verdict: "Verdict: Strong" / "Verdict: Needs Work" / "Verdict: Weak".

Keep it tight — this gets read back to them out loud. No preamble, no headers, just the feedback."""

app = Flask(__name__, static_folder="static")

_service = None


def get_service():
    global _service
    if _service is None:
        _service = build("docs", "v1", credentials=get_creds())
    return _service


def _iter_paragraphs(tab: dict):
    """Yield (text, fully_bold) for each non-empty paragraph in a tab.

    Headings and 'Q: ...' labels are written as fully-bold paragraphs (see
    build_write_requests in agent.py); body/answer text is not. Plain-text
    extraction alone can't tell a heading from an answer line, so we need
    the bold flag straight from the API response to split sections correctly.
    """
    body = tab.get("documentTab", {}).get("body", {})
    for block in body.get("content", []):
        para = block.get("paragraph")
        if not para:
            continue
        text = "".join(e.get("textRun", {}).get("content", "") for e in para.get("elements", []))
        stripped = text.strip()
        if not stripped:
            continue
        runs = [
            e["textRun"] for e in para.get("elements", [])
            if e.get("textRun", {}).get("content", "").strip()
        ]
        fully_bold = bool(runs) and all(r.get("textStyle", {}).get("bold") for r in runs)
        yield stripped, fully_bold


def parse_qa_pairs(tab: dict) -> list[dict]:
    """Bold alone can't tell a topic heading ('Overfitting') apart from a
    bold structural label inside an answer ('Approach -> Key Decisions ->
    Trade-offs', 'Interview answer:') — both are fully-bold paragraphs with
    no other distinguishing style. The two are told apart by what follows:
    a heading is immediately followed by the next 'Q:' line; an in-answer
    label is followed by more answer content. Hence the one-paragraph
    lookahead below instead of a single streaming pass.
    """
    paras = list(_iter_paragraphs(tab))
    pairs = []
    question = None
    answer_lines = []

    def flush():
        if question is not None:
            pairs.append({
                "id": len(pairs),
                "question": question,
                "answer": "\n".join(answer_lines).strip(),
            })

    for i, (text, fully_bold) in enumerate(paras):
        if fully_bold and text.startswith("Q:"):
            flush()
            question = text[2:].strip()
            answer_lines = []
            continue

        if fully_bold:
            next_is_question = i + 1 < len(paras) and paras[i + 1][1] and paras[i + 1][0].startswith("Q:")
            if next_is_question:
                # Topic heading right before the next question — belongs to neither answer.
                flush()
                question = None
                answer_lines = []
                continue
            # Structural label (e.g. "Approach -> Key Decisions -> Trade-offs") — keep with current answer.

        if question is not None:
            answer_lines.append(text)
    flush()

    return pairs


@app.route("/")
def index():
    return send_from_directory("static", "practice.html")


@app.route("/api/tabs")
def api_tabs():
    try:
        children = get_child_tabs_of(get_service(), DOC_ID, PARENT_TAB_NAME)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify([{"id": tab_id, "title": title} for tab_id, title in children])


@app.route("/api/questions")
def api_questions():
    tab_id = request.args.get("tab_id")
    if not tab_id:
        return jsonify({"error": "tab_id is required"}), 400

    doc_full = get_service().documents().get(documentId=DOC_ID, includeTabsContent=True).execute()
    try:
        tab = find_tab_in(doc_full.get("tabs", []), tab_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    pairs = parse_qa_pairs(tab)
    if not pairs:
        return jsonify({"error": "No 'Q:' formatted questions found in this tab"}), 404
    return jsonify(pairs)


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    expected_answer = (data.get("expected_answer") or "").strip()
    user_answer = (data.get("user_answer") or "").strip()

    if not question or not user_answer:
        return jsonify({"error": "question and user_answer are required"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY is not set"}), 500

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=FEEDBACK_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Prep notes (their prepared answer):\n{expected_answer or '[none written]'}\n\n"
                    f"What they said out loud (transcribed):\n{user_answer}"
                ),
            }],
        )
    except anthropic.APIStatusError as e:
        detail = e.body.get("error", {}).get("message") if isinstance(e.body, dict) else None
        return jsonify({"error": detail or str(e)}), 502

    return jsonify({"feedback": message.content[0].text})


if __name__ == "__main__":
    require_doc_id()
    app.run(port=5050, debug=True)
