import os
import json
import sqlite3
import requests
import streamlit as st
from dotenv import load_dotenv

# Load API key
load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or st.secrets.get("OPENROUTER_API_KEY")
MODEL = "openchat/openchat-3.5-0106"

st.set_page_config(page_title="Patent Categorizer (OpenRouter)", layout="centered")
st.title("🔍 Patent Categorization Tool (Open Source LLM)")

st.write(f"🔑 API Key loaded: {OPENROUTER_API_KEY[:10]}..." if OPENROUTER_API_KEY else "❌ API Key not loaded")
st.write(f"🧠 Model in use: {MODEL}")

DB_FILE = "patents_cache.db"
SEARCH_URL = "https://search.patentsview.org/api/v1/patent"

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
                print("[DEBUG] Cache HIT")
                return json.loads(row[0])
            else:
                print("[DEBUG] Cache MISS")
    except Exception as e:
        st.error(f"❌ Cache error: {e}")
        print(f"[DEBUG] Cache error: {e}")

    query = {
        "q": f"{field_type}:{normalized_number}",
        "fl": [
            "patent_id", "patent_number", "patent_title", "patent_abstract", "patent_date",
            "application_number", "app_date", "assignee_organization", "inventor_name_first", "inventor_name_last",
            "publication_number", "publication_date"
        ],
        "sort": [{"patent_date": "desc"}]
    }

    try:
        print(f"[DEBUG] Querying PatentsView API with:\n{json.dumps(query, indent=2)}")
        response = requests.post(SEARCH_URL, json=query, timeout=10)
        print(f"[DEBUG] Status code: {response.status_code}")
        print(f"[DEBUG] Response body: {response.text[:300]}")

        if response.status_code == 200:
            data = response.json()
            if "patents" in data and data["patents"]:
                print("[DEBUG] Patent data found.")
                normalized_data = {"patents": []}
                for p in data["patents"]:
                    normalized_patent = {
                        "patent_number": p.get("patent_id") or p.get("patent_number") or p.get("publication_number"),
                        "patent_title": p.get("patent_title"),
                        "patent_abstract": p.get("patent_abstract"),
                        "patent_date": p.get("patent_date") or p.get("publication_date"),
                        "filing_date": p.get("app_date"),
                        "application_number": p.get("application_number"),
                        "publication_number": p.get("publication_number"),
                        "assignee_organization": p.get("assignee_organization"),
                        "inventor_first_name": p.get("inventor_name_first"),
                        "inventor_last_name": p.get("inventor_name_last"),
                        "patent_type": patent_type
                    }
                    normalized_data["patents"].append(normalized_patent)

                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute("REPLACE INTO patent_cache (patent_number, data_json) VALUES (?, ?)",
                                 (cache_key, json.dumps(normalized_data)))
                return normalized_data
            else:
                st.warning(f"⚠️ No patent data found for '{normalized_number}' ({field_type}).")
                return None
        else:
            st.error(f"❌ API error ({response.status_code}): {response.text[:300]}")
            return None

    except requests.exceptions.Timeout:
        st.error("❌ Request timed out while querying PatentsView API.")
        return None
    except requests.exceptions.RequestException as e:
        st.error(f"❌ Network error: {e}")
        return None
    except Exception as e:
        st.error(f"❌ Unexpected error: {e}")
        return None

# --- OpenRouter LLM Call ---
def call_openrouter_llm(prompt):
    if not OPENROUTER_API_KEY:
        raise Exception("OpenRouter API key not found. Please set it in .env or Streamlit secrets.")

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a patent analyst. Always return valid JSON."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 1024
    }

    print(f"[DEBUG] Sending request to OpenRouter...")
    print(f"[DEBUG] Headers: {headers}")
    print(f"[DEBUG] Payload: {json.dumps(payload, indent=2)[:300]}")

    response = requests.post(url, headers=headers, json=payload)
    print(f"[DEBUG] OpenRouter status: {response.status_code}")
    print(f"[DEBUG] OpenRouter response: {response.text[:300]}")

    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        raise Exception(f"OpenRouter API Error: {response.status_code}, {response.text}")

# --- Categorization with LLM ---
def categorize_with_llm(patent_data):
    patent = patent_data['patents'][0]
    title = patent.get('patent_title', '')
    abstract = patent.get('patent_abstract', '')
    patent_number = patent.get('patent_number', '')

    prompt = f"""
You are a patent analyst. Given the patent title and abstract below, categorize the patent and provide analysis.

Title: {title}
Abstract: {abstract}

Return ONLY a valid JSON object with these exact keys:
{{
    "technology_areas": ["area1", "area2"],
    "primary_category": "main category",
    "ipc_predicted": ["predicted IPC codes"],
    "cpc_predicted": ["predicted CPC codes"],
    "uspc_predicted": ["predicted USPC codes"],
    "reasoning": "Brief explanation of categorization"
}}
"""
    try:
        response_text = call_openrouter_llm(prompt)
        response_text = response_text.strip().removeprefix("```json").removesuffix("```").strip()
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse LLM response as JSON: {e}", "raw_response": response_text[:500]}
    except Exception as e:
        return {"error": str(e)}

# --- Streamlit UI ---
init_cache()

patent_type = st.selectbox("Select patent type:", ["Granted Patent", "Patent Application"])
patent_input = st.text_input("Enter US Patent/Application Number:", placeholder="e.g., 6172354 or 20230123456")

if st.button("Submit"):
    with st.spinner("Fetching patent data and analyzing..."):
        data = query_patent(patent_input, patent_type)
        if not data:
            st.error("❌ Patent not found or API error.")
        else:
            st.subheader("📄 Patent Metadata")
            patent = data['patents'][0]
            st.write("**Title:**", patent.get("patent_title"))
            st.write("**Abstract:**", patent.get("patent_abstract"))
            st.write("**Filing Date:**", patent.get("filing_date"))
            st.write("**Grant/Publication Date:**", patent.get("patent_date"))

            st.subheader("🤖 AI Categorization")
            result = categorize_with_llm(data)
            if "error" in result:
                st.error(f"LLM Error: {result['error']}")
                if "raw_response" in result:
                    st.code(result["raw_response"])
            else:
                st.json(result)
