import os
import json
import sqlite3
import requests
import streamlit as st
from dotenv import load_dotenv

# Load API key
load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or st.secrets.get("OPENROUTER_API_KEY")
MODEL = "meta-llama/llama-3-70b-instruct"

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

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        raise Exception(f"OpenRouter API Error: {response.status_code}, {response.text}")

# --- LLM Categorization ---
def categorize_with_llm(patent_data):
    patent = patent_data['patents'][0]
    title = patent.get('patent_title', '')
    abstract = patent.get('patent_abstract', '')
    patent_number = patent.get('patent_number', '')

    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT gpt_json FROM patent_cache WHERE patent_number=?", (patent_number,)).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass

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
        response_text = call_openrouter_llm(prompt).strip()
        if response_text.startswith('```json'):
            response_text = response_text[7:]
        if response_text.endswith('```'):
            response_text = response_text[:-3]
        result = json.loads(response_text.strip())
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("UPDATE patent_cache SET gpt_json=? WHERE patent_number=?",
                         (json.dumps(result), patent_number))
        return result
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse LLM response as JSON: {e}", "raw_response": response_text[:500]}
    except Exception as e:
        return {"error": str(e)}

# --- Streamlit UI ---
st.set_page_config(page_title="Patent Categorizer (LLaMA 3 via OpenRouter)", layout="centered")
st.title("🔍 Patent Categorization Tool (Open Source LLM via OpenRouter)")

init_cache()

patent_type = st.selectbox("Select patent type:", ["Granted Patent", "Patent Application"],
                           help="Granted patents have numbers like 6172354. Applications like 20230123456 or 16/123,456")

col1, col2 = st.columns([3, 1])
with col1:
    patent_input = st.text_input("Enter Patent Number:", placeholder="e.g., 6172354 or 20230123456",
                                  help="Remove any US/B1/A1 formatting, just enter the core number.")
with col2:
    if st.button("Use Test Patent"):
        patent_input = "6172354" if patent_type == "Granted Patent" else "20200123456"

show_debug = st.checkbox("Show debug information")

if patent_input:
    with st.spinner("Fetching patent data..."):
        result = query_patent(patent_input, patent_type)

        if not result:
            st.error("❌ Patent not found or error encountered.")
        else:
            st.success("✅ Patent data retrieved.")
            st.subheader("Patent Metadata")
            st.json(result)

            gpt_result = categorize_with_llm(result)

            if "error" in gpt_result:
                st.error(f"LLM Error: {gpt_result['error']}")
                if "raw_response" in gpt_result:
                    st.code(gpt_result["raw_response"])
            else:
                st.subheader("🤖 AI-Powered Categorization")
                col1, col2 = st.columns(2)
                with col1:
                    st.write("**Technology Areas:**")
                    for area in gpt_result.get("technology_areas", []):
                        st.write(f"• {area}")
                    st.write("**Primary Category:**", gpt_result.get("primary_category", "N/A"))
                with col2:
                    st.write("**Predicted IPC Codes:**")
                    for code in gpt_result.get("ipc_predicted", []):
                        st.write(f"• {code}")
                    st.write("**Predicted CPC Codes:**")
                    for code in gpt_result.get("cpc_predicted", []):
                        st.write(f"• {code}")
                st.subheader("🧠 AI Reasoning")
                st.write(gpt_result.get("reasoning", "No explanation provided."))
                with st.expander("View Full JSON Response"):
                    st.json(gpt_result)

            if show_debug:
                st.code(json.dumps(result, indent=2)[:1000])
