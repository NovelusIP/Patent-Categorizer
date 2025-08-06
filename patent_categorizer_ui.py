import os
import json
import sqlite3
import requests
import streamlit as st
from dotenv import load_dotenv

# Load API key
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or st.secrets.get("GROQ_API_KEY")
MODEL = "llama3-70b-8192"

st.set_page_config(page_title="Patent Categorizer (Groq)", layout="centered")
st.title("🔍 Patent Categorization Tool (Open Source LLM via Groq)")

st.write(f"🔑 API Key loaded: {'Yes' if GROQ_API_KEY else 'No'}")
st.write(f"🧠 Model in use: {MODEL}")

DB_FILE = "patents_cache.db"
SEARCH_URL = "https://search.patentsview.org/api/v1/patent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CPC_SECTIONS = {
    "A": "Human Necessities",
    "B": "Performing Operations; Transporting",
    "C": "Chemistry; Metallurgy",
    "D": "Textiles; Paper",
    "E": "Fixed Constructions",
    "F": "Mechanical Engineering; Lighting; Heating; Weapons; Blasting",
    "G": "Physics",
    "H": "Electricity",
    "Y": "General Tagging of New Technologies"
}

# --- Initialize SQLite Cache ---
def init_cache():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS patent_cache (
            patent_number TEXT PRIMARY KEY,
            data_json TEXT,
            gpt_json TEXT
        )
        """)

# --- Normalize Input ---
def normalize_patent_number(patent_input, patent_type):
    clean_input = patent_input.strip().replace(",", "").replace("/", "").replace("-", "")
    if patent_type == "Patent Application":
        if len(clean_input) == 11 and clean_input.startswith("20"):
            return clean_input, "publication_number"
        elif len(clean_input) >= 7:
            return clean_input, "application_number"
        else:
            return clean_input, "application_number"
    else:
        clean_input = clean_input.replace("US", "").replace("B1", "").replace("B2", "").replace("A1", "")
        return clean_input.strip(), "patent_number"

# --- Query New PatentsView API ---
def query_patent(patent_input, patent_type):
    normalized_number, field_type = normalize_patent_number(patent_input, patent_type)
    cache_key = f"{patent_type}_{normalized_number}"

    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute("SELECT data_json FROM patent_cache WHERE patent_number=?", (cache_key,)).fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        st.error(f"❌ Cache error: {e}")

    query = {
        "q": f"{field_type}:{normalized_number}",
        "fl": [
            "patent_id", "patent_number", "patent_title", "patent_abstract", "patent_date",
            "application_number", "app_date", "assignee_organization", "inventor_name_first", "inventor_name_last",
            "publication_number", "publication_date", "cpc_subgroup_id", "ipc_class_symbol", "uspc_class",
            "patent_priority_date", "patent_num_cited_by_us_patents", "claim_statement"
        ],
        "sort": [{"patent_date": "desc"}]
    }

    try:
        response = requests.post(SEARCH_URL, json=query, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "patents" in data and data["patents"]:
                normalized_data = {"patents": []}
                for p in data["patents"]:
                    normalized_patent = {
                        "patent_number": p.get("patent_id") or p.get("patent_number") or p.get("publication_number"),
                        "patent_title": p.get("patent_title"),
                        "patent_abstract": p.get("patent_abstract"),
                        "patent_date": p.get("patent_date") or p.get("publication_date"),
                        "filing_date": p.get("app_date"),
                        "priority_date": p.get("patent_priority_date"),
                        "application_number": p.get("application_number"),
                        "publication_number": p.get("publication_number"),
                        "assignee_organization": p.get("assignee_organization"),
                        "inventor_first_name": p.get("inventor_name_first"),
                        "inventor_last_name": p.get("inventor_name_last"),
                        "cpc_codes": p.get("cpc_subgroup_id", []),
                        "ipc_codes": p.get("ipc_class_symbol", []),
                        "uspc_codes": p.get("uspc_class", []),
                        "citations": p.get("patent_num_cited_by_us_patents"),
                        "claims": p.get("claim_statement"),
                        "patent_type": patent_type
                    }
                    normalized_data["patents"].append(normalized_patent)

                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute("REPLACE INTO patent_cache (patent_number, data_json) VALUES (?, ?)",
                                 (cache_key, json.dumps(normalized_data)))
                return normalized_data
            else:
                return None
        else:
            st.error(f"❌ API error ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        st.error(f"❌ Query error: {e}")
        return None

# --- Parse CPC Codes ---
def parse_cpc(cpc_list):
    parsed = []
    for code in cpc_list:
        if not code or len(code) < 4:
            continue
        section = code[0]
        class_ = code[1:3]
        subclass = code[3]
        rest = code[4:]
        parsed.append({
            "raw": code,
            "section": section,
            "section_name": CPC_SECTIONS.get(section, "Unknown"),
            "class": class_,
            "subclass": subclass,
            "rest": rest
        })
    return parsed

# --- Try LLM Categorization ---
def try_llm_categorization(title, abstract):
    try:
        if not GROQ_API_KEY:
            raise Exception("Missing API Key")

        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You are a patent analyst. Always return valid JSON."},
                {"role": "user", "content": f"Title: {title}\nAbstract: {abstract}\nReturn JSON with technology_areas, primary_category, cpc_predicted, ipc_predicted, reasoning."}
            ]
        }
        response = requests.post(GROQ_URL, headers=headers, json=payload)
        if response.status_code == 200:
            return json.loads(response.json()["choices"][0]["message"]["content"]), None
        else:
            return None, f"LLM API Error {response.status_code}: {response.text}"
    except Exception as e:
        return None, f"LLM Exception: {str(e)}"

# --- Streamlit UI ---
init_cache()

patent_type = st.selectbox("Select patent type:", ["Granted Patent", "Patent Application"])
patent_input = st.text_input("Enter US Patent/Application Number:", placeholder="e.g., 6172354 or 20230123456")

if st.button("Submit"):
    with st.spinner("Fetching and analyzing patent data..."):
        data = query_patent(patent_input, patent_type)
        if not data:
            st.error("❌ Patent not found or data error.")
        else:
            patent = data['patents'][0]
            title = patent.get("patent_title", "")
            abstract = patent.get("patent_abstract", "")

            st.subheader("📄 Patent Metadata")
            st.json(patent)

            llm_result, llm_error = try_llm_categorization(title, abstract)

            if llm_result:
                st.subheader("🤖 LLM Categorization Result")
                st.success("✅ LLM categorization successful")
                st.json(llm_result)
            else:
                st.subheader("🔁 Fallback Categorization (LLM unavailable)")
                st.info("Using fallback logic due to LLM failure.")

                fallback_result = {
                    "title": title,
                    "abstract": abstract,
                    "dates": {
                        "filing_date": patent.get("filing_date"),
                        "publication_date": patent.get("patent_date"),
                        "priority_date": patent.get("priority_date")
                    },
                    "inventors": list(zip(
                        patent.get("inventor_first_name", []),
                        patent.get("inventor_last_name", [])
                    )),
                    "assignees": patent.get("assignee_organization", []),
                    "cpc_codes": parse_cpc(patent.get("cpc_codes", [])),
                    "ipc_codes": patent.get("ipc_codes", []),
                    "uspc_codes": patent.get("uspc_codes", []),
                    "citations": patent.get("citations"),
                    "claims": patent.get("claims"),
                    "source": "fallback"
                }
                st.json(fallback_result)

                with st.expander("LLM Failure Details"):
                    st.code(llm_error)
