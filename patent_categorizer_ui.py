
import os
import json
import sqlite3
import requests
import streamlit as st
from dotenv import load_dotenv

# Load Together API key from .env or Streamlit secrets
load_dotenv()
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY") or st.secrets["TOGETHER_API_KEY"]

# SQLite DB setup
DB_FILE = "patents_cache.db"

def init_cache():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS patent_cache (
            patent_number TEXT PRIMARY KEY,
            data_json TEXT,
            gpt_json TEXT
        )
        """)

# PatentsView API
BASE_URL = "https://api.patentsview.org/patents/query"

def query_patent(patent_number):
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT data_json FROM patent_cache WHERE patent_number=?", (patent_number,)).fetchone()
        if row:
            return json.loads(row[0])

    query = {
        "q": {"_eq": {"patent_number": patent_number}},
        "f": [
            "patent_number", "patent_title", "abstract", "patent_date",
            "application_number", "filing_date", "cpc_subgroup_id",
            "ipc_subgroup_id", "uspc_mainclass_id",
            "assignees", "inventors"
        ]
    }
    res = requests.get(BASE_URL, params={"q": json.dumps(query)})
    if res.status_code == 200:
        data = res.json()
        if "patents" in data and data["patents"]:
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute("REPLACE INTO patent_cache (patent_number, data_json) VALUES (?, ?)",
                             (patent_number, json.dumps(data)))
            return data
    return None

def call_together_llama3(prompt):
    url = "https://api.together.xyz/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {TOGETHER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "meta-llama/Meta-Llama-3-70B-Instruct-Turbo",
        "messages": [
            {"role": "system", "content": "You are a patent analyst."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 1024
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        raise Exception(f"Together API Error: {response.status_code}, {response.text}")

def categorize_with_llm(patent_data):
    patent = patent_data['patents'][0]
    title = patent.get('patent_title', '')
    abstract = patent.get('abstract', '')
    prompt = f"""
You are a patent analyst. Given the patent title and abstract, categorize the patent using CPC, IPC, and USPC codes if applicable. 
Provide also high-level technology categories (like AI, biotech, mechanical, etc). Include a paragraph of reasoning.

Title: {title}
Abstract: {abstract}

Return only JSON with:
- technology_areas
- ipc_predicted
- cpc_predicted
- uspc_predicted
- reasoning
"""

    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT gpt_json FROM patent_cache WHERE patent_number=?", (patent.get("patent_number"),)).fetchone()
        if row:
            return json.loads(row[0])

    try:
        response_text = call_together_llama3(prompt)
        result = json.loads(response_text)
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("UPDATE patent_cache SET gpt_json=? WHERE patent_number=?",
                         (json.dumps(result), patent.get("patent_number")))
        return result
    except Exception as e:
        return {"error": str(e)}

# Streamlit UI
st.set_page_config(page_title="Patent Categorizer (LLaMA 3 via Together.ai)", layout="centered")
st.title("🔍 Patent Categorization Tool (Open Source LLM)")

init_cache()

patent_input = st.text_input("Enter US Patent Number (e.g., 11234567)")

if patent_input:
    with st.spinner("Fetching and analyzing patent..."):
        patent_data = query_patent(patent_input.strip())
        if not patent_data:
            st.error("Patent not found or API error.")
        else:
            gpt_result = categorize_with_llm(patent_data)
            patent = patent_data['patents'][0]

            st.subheader("📄 Patent Metadata")
            st.write({
                "Title": patent.get("patent_title"),
                "Abstract": patent.get("abstract"),
                "Filing Date": patent.get("filing_date"),
                "Publication Date": patent.get("patent_date"),
                "Application Number": patent.get("application_number"),
                "Assignee(s)": [a.get("assignee_organization") for a in patent.get("assignees", [])],
                "Inventor(s)": [f"{i.get('inventor_first_name')} {i.get('inventor_last_name')}" for i in patent.get("inventors", [])]
            })

            if "error" in gpt_result:
                st.error(f"LLM Error: {gpt_result['error']}")
            else:
                st.subheader("📚 Categorization")
                st.json(gpt_result)
                st.markdown("### 🧠 Reasoning")
                st.write(gpt_result.get("reasoning", "No explanation returned."))
