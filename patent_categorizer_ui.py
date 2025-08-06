import os
import json
import sqlite3
import requests
import streamlit as st
from dotenv import load_dotenv

# Load Together API key from .env or Streamlit secrets
load_dotenv()
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY") or st.secrets.get("TOGETHER_API_KEY")

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

# PatentsView API - FIXED VERSION
BASE_URL = "https://api.patentsview.org/patents/query"

def query_patent(patent_number):
    # Check cache first
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT data_json FROM patent_cache WHERE patent_number=?", (patent_number,)).fetchone()
        if row:
            return json.loads(row[0])

    # Fixed query structure
    query = {
        "q": {"patent_number": patent_number},  # Simplified query
        "f": [
            "patent_number", "patent_title", "patent_abstract", "patent_date",
            "application_number", "filing_date", "cpc_subgroup_id",
            "ipc_subgroup_id", "uspc_mainclass_id",
            "assignee_organization", "inventor_first_name", "inventor_last_name"
        ]
    }
    
    try:
        # Use POST request with JSON data
        response = requests.post(BASE_URL, json=query, timeout=10)
        print(f"API Response Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"API Response: {json.dumps(data, indent=2)[:500]}...")  # Debug output
            
            if "patents" in data and data["patents"]:
                # Cache the result
                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute("REPLACE INTO patent_cache (patent_number, data_json) VALUES (?, ?)",
                                 (patent_number, json.dumps(data)))
                return data
            else:
                print(f"No patents found in response for patent number: {patent_number}")
                return None
        else:
            print(f"API Error: {response.status_code}, {response.text}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Request Exception: {e}")
        return None

def call_together_llama3(prompt):
    if not TOGETHER_API_KEY:
        raise Exception("Together API key not found. Please set TOGETHER_API_KEY in .env or Streamlit secrets.")
    
    url = "https://api.together.xyz/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {TOGETHER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "meta-llama/Meta-Llama-3-70B-Instruct-Turbo",
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
        raise Exception(f"Together API Error: {response.status_code}, {response.text}")

def categorize_with_llm(patent_data):
    patent = patent_data['patents'][0]
    title = patent.get('patent_title', '')
    abstract = patent.get('patent_abstract', '')  # Fixed field name
    patent_number = patent.get('patent_number', '')
    
    # Check cache first
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT gpt_json FROM patent_cache WHERE patent_number=?", (patent_number,)).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass  # Cache corrupted, proceed with API call

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
        response_text = call_together_llama3(prompt)
        
        # Clean up response (remove markdown formatting if present)
        response_text = response_text.strip()
        if response_text.startswith('```json'):
            response_text = response_text[7:]
        if response_text.endswith('```'):
            response_text = response_text[:-3]
        response_text = response_text.strip()
        
        result = json.loads(response_text)
        
        # Cache the result
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("UPDATE patent_cache SET gpt_json=? WHERE patent_number=?",
                         (json.dumps(result), patent_number))
        return result
        
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse LLM response as JSON: {e}", "raw_response": response_text[:500]}
    except Exception as e:
        return {"error": str(e)}

# Streamlit UI
st.set_page_config(page_title="Patent Categorizer (LLaMA 3 via Together.ai)", layout="centered")
st.title("🔍 Patent Categorization Tool (Open Source LLM)")

init_cache()

patent_input = st.text_input("Enter US Patent Number (e.g., 11234567, 10123456)")

if patent_input:
    with st.spinner("Fetching and analyzing patent..."):
        patent_data = query_patent(patent_input.strip())
        
        if not patent_data:
            st.error("Patent not found or API error. Please check the patent number and try again.")
            st.info("Try a patent number like: 11234567, 10123456, or 9876543")
        else:
            patent = patent_data['patents'][0]
            
            st.subheader("📄 Patent Metadata")
            
            # Display basic patent info
            col1, col2 = st.columns(2)
            with col1:
                st.write("**Title:**", patent.get("patent_title", "N/A"))
                st.write("**Patent Number:**", patent.get("patent_number", "N/A"))
                st.write("**Filing Date:**", patent.get("filing_date", "N/A"))
                st.write("**Publication Date:**", patent.get("patent_date", "N/A"))
            
            with col2:
                st.write("**Application Number:**", patent.get("application_number", "N/A"))
                assignees = patent.get("assignee_organization", [])
                if isinstance(assignees, list):
                    st.write("**Assignee(s):**", ", ".join(assignees) if assignees else "N/A")
                else:
                    st.write("**Assignee:**", assignees or "N/A")
            
            # Abstract
            abstract = patent.get("patent_abstract", "No abstract available")
            st.write("**Abstract:**")
            st.text_area("", abstract, height=100, disabled=True)
            
            # LLM Categorization
            gpt_result = categorize_with_llm(patent_data)
            
            if "error" in gpt_result:
                st.error(f"LLM Error: {gpt_result['error']}")
                if "raw_response" in gpt_result:
                    st.code(gpt_result["raw_response"])
            else:
                st.subheader("🤖 AI-Powered Categorization")
                
                # Display categorization results
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
                
                # Show full JSON in expander
                with st.expander("View Full JSON Response"):
                    st.json(gpt_result)
