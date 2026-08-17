# AI Database Query Guide: Local Graduate-Entrance Mathematics Question Bank

> [!NOTE]
> **To any AI Agent (Claude, Cursor, Cline, etc.) reading this guide:**
> You are operating within a local workspace equipped with an SQLite-based graduate-entrance mathematics question bank (`math_question_bank.db`).
> Instead of reading raw files or loading massive markdown lists that overflow your context window, you should **proactively execute the local CLI module `scripts.search_questions`** in the terminal to search, retrieve, and format questions with 100% precision.

---

## 1. Quick Start: The CLI Search Tool (`scripts/search_questions.py`)

From the project root, run `python3 -m scripts.search_questions` to fuzzy search questions. It automatically returns beautifully formatted Markdown with LaTeX formulas fully preserved.

### Parameter Reference
| Option | Long Option | Description | Example / Allowed Values |
| :--- | :--- | :--- | :--- |
| `-q` | `--query` | Fuzzy search keyword (matches exam track, subject, topic, or content). | `-q "矩阵"` or `-q "极限"` |
| `-n` | `--limit` | Maximum number of questions to return. **Use `-1` for NO LIMIT.** | `-n 50` or `-n -1` (default: 50) |
| `-a` | `--with-answers` | Flag to include answers, step-by-step explanations, and reviews. | (Omitting this hides answers) |
| `-t` | `--type` | Filter by question type. | `single_choice`, `fill_in_blank`, `detailed_answer` |
| `-d` | `--difficulty` | Filter by difficulty level. | `basic`, `standard`, `comprehensive`, `advanced` |
| `-r` | `--related-to` | Fetch all questions linked to a specific Question ID. | `-r 3` |

---

## 2. Dynamic Search Examples (Copy & Execute)

### 📌 Case A: Generate Graduate-Math Practice Sheet (No Answers)
To find questions in **数学一 / 高等数学 / 一元函数微分学** without leaking answers:
```bash
python3 -m scripts.search_questions -q "一元函数微分学" -n -1
```

### 📌 Case B: Generate a Solution Note (With Answers)
To retrieve **3 advanced linear-algebra questions** with detailed derivations and reviews:
```bash
python3 -m scripts.search_questions -q "线性代数" -d "advanced" -n 3 -a
```

### 📌 Case C: Find Linked / Variation Questions
To grab all variations or linked sub-questions associated with a known question ID (e.g., ID `#3`):
```bash
python3 -m scripts.search_questions -r 3 -a
```

---

## 3. Core Database Table Schema (For Text-to-SQL / Custom Queries)

If you are a advanced Agent authorized to query the SQLite database (`math_question_bank.db`) directly using Python's `sqlite3` or SQLAlchemy, utilize this exact DDL structure of the `questions` table:

```sql
CREATE TABLE questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,                  -- Question stem (LaTeX + Markdown mixed)
    question_type VARCHAR(50),              -- Type: single_choice, fill_in_blank, detailed_answer
    exam_track VARCHAR(100),                -- Exam track: "数学一", "数学二", "数学三"
    subject VARCHAR(100),                   -- Subject: "高等数学", "线性代数", "概率论与数理统计"
    topic VARCHAR(100),                     -- Topic: e.g. "一元函数微分学"
    difficulty VARCHAR(50),                 -- Difficulty: basic, standard, comprehensive, advanced
    source VARCHAR(200),                    -- Source / exam origin, e.g. "考研数学真题"
    answer_markdown TEXT,                   -- Answers & Explanations (LaTeX + Markdown mixed)
    review TEXT,                            -- Teacher's review / comments (can be blank)
    association_group_id VARCHAR(100),      -- Bi-directional grouping token for associated variations
    image_paths TEXT,                       -- JSON string list of local relative image paths
    created_at DATETIME
);
```

---

## 4. Prompt Recipes for Users to Instruct AI

When you want your AI assistant to generate rigorous solutions or graduate-math exam sheets, simply paste one of these prompts:

### 💬 Solution Note Generation Prompt
> "Please read `docs/AI_DATABASE_GUIDE.md` first. Then, run `python3 -m scripts.search_questions -q \"矩阵\" -n 3 -a`. Use the returned questions and answers to draft a rigorous graduate-entrance mathematics solution note."

### 💬 Student Worksheet Generation Prompt
> "Read `docs/AI_DATABASE_GUIDE.md`. Run `python3 -m scripts.search_questions -q "一元函数积分学" -n -1` to fetch relevant questions. Select 5 to assemble a clean graduate-math practice sheet (do not include answers)."
