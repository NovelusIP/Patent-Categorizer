import os
import json
import sqlite3
import requests
import streamlit as st
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# Load API key
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or st.secrets.get("GROQ_API_KEY")
MODEL = "llama3-70b-8192"

st.set_page_config(page_title="Patent Categorizer (Groq)", layout="centered")
st.markdown("""
    <h2 style='font-family: Arial; color: #003366;'>Patent Categorization Tool</h2>
""", unsafe_allow_html=True)

st.markdown(f"<p style='font-size:14px;'>API Key loaded: <strong>{'Yes' if GROQ_API_KEY else 'No'}</strong></p>", unsafe_allow_html=True)
st.markdown(f"<p style='font-size:14px;'>Model in use: <strong>{MODEL}</strong></p>", unsafe_allow_html=True)

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

# --- Fallback Google Patents Scraper ---
def scrape_google_patents(patent_number):
    try:
        url = f"https://patents.google.com/patent/US{patent_number}/en"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None, f"Google Patents HTTP error {response.status_code}"

        soup = BeautifulSoup(response.text, 'html.parser')
        title = soup.find("meta", {"name": "DC.title"})
        abstract = soup.find("meta", {"name": "DC.description"})
        return {
            "title": title["content"] if title else "",
            "abstract": abstract["content"] if abstract else "",
            "source": "google_patents"
        }, None
    except Exception as e:
        return None, f"Scraping error: {e}"

# --- Query PatentsView API ---
def query_patent(patent_input, patent_type):
    normalized_number, field_type = normalize_patent_number(patent_input, patent_type)
    cache_key = f"{patent_type}_{normalized_number}"

    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute("SELECT data_json FROM patent_cache WHERE patent_number=?", (cache_key,)).fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        st.error(f"Cache error: {e}")

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

    headers = {
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(SEARCH_URL, headers=headers, json=query, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "patents" in data and data["patents"]:
                return {"patents": data["patents"]}
            else:
                st.info("PatentsView returned no results. Falling back to Google Patents.")
                fallback_data, scrape_error = scrape_google_patents(normalized_number)
                if fallback_data:
                    return {"patents": [
                        {
                            "patent_number": normalized_number,
                            "patent_title": fallback_data["title"],
                            "patent_abstract": fallback_data["abstract"],
                            "source": "google_patents"
                        }
                    ]}
                else:
                    st.error(f"Google Patents fallback failed: {scrape_error}")
                    return None
        else:
            st.warning("PatentsView API failed. Falling back to Google Patents.")
            fallback_data, scrape_error = scrape_google_patents(normalized_number)
            if fallback_data:
                return {"patents": [
                    {
                        "patent_number": normalized_number,
                        "patent_title": fallback_data["title"],
                        "patent_abstract": fallback_data["abstract"],
                        "source": "google_patents"
                    }
                ]}
            else:
                st.error(f"Google Patents fallback failed: {scrape_error}")
                return None
    except Exception as e:
        st.error(f"Query error: {e}")
        return None

# UI
init_cache()
patent_type = st.selectbox("Select patent type:", ["Granted Patent", "Patent Application"])
patent_input = st.text_input("Enter US Patent/Application Number:", placeholder="e.g., 6172354 or 20230123456")

if st.button("Submit"):
    with st.spinner("Fetching and analyzing patent data..."):
        data = query_patent(patent_input, patent_type)
        if not data:
            st.error("Patent not found or data error.")
        else:
            patent = data['patents'][0]
            if patent.get("source") == "google_patents":
                st.warning("Using Google Patents fallback data due to PatentsView API failure.")

            st.markdown("""
                <h4 style='color: #1a1a1a;'>Patent Analysis</h4>
            """, unsafe_allow_html=True)
            st.json(patent)

            if not GROQ_API_KEY:
                st.warning("GROQ API key missing. Skipping LLM categorization.")
            else:
                try:
                    headers = {
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json"
                    }
                    prompt = f"Categorize the following patent: {patent['patent_title']}\n\nAbstract: {patent['patent_abstract']}"
                    payload = {
                        "model": MODEL,
                        "messages": [
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.3,
                        "max_tokens": 500
                    }
                    response = requests.post(GROQ_URL, headers=headers, json=payload)
                    if response.status_code == 200:
                        result = response.json()["choices"][0]["message"]["content"]
                        st.subheader("LLM Categorization Result")
                        st.markdown(result)
                    else:
                        st.warning("LLM access denied (403). Using fallback only.")
                except Exception as e:
                    st.error(f"LLM call failed: {e}. Using fallback only.")
