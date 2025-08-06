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

# PatentsView API endpoints
LEGACY_URL = "https://api.patentsview.org/patents/query"
SEARCH_URL = "https://search.patentsview.org/api/v1/patent"

def normalize_patent_number(patent_input, patent_type):
    """
    Normalize patent numbers for API queries
    """
    clean_input = patent_input.strip().replace(",", "").replace("/", "").replace("-", "")
    
    if patent_type == "Patent Application":
        # Handle different application number formats
        if len(clean_input) == 11 and clean_input.startswith("20"):
            # Published application: 20230123456
            return clean_input, "publication_number"
        elif len(clean_input) >= 7:
            # Application number: 16123456 or similar
            return clean_input, "application_number"
        else:
            return clean_input, "application_number"
    else:
        # Granted patent - remove any prefixes/suffixes
        clean_input = clean_input.replace("US", "").replace("B1", "").replace("B2", "").replace("A1", "")
        return clean_input.strip(), "patent_number"

def query_patent(patent_input, patent_type):
    normalized_number, field_type = normalize_patent_number(patent_input, patent_type)
    
    # Check cache first
    cache_key = f"{patent_type}_{normalized_number}"
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT data_json FROM patent_cache WHERE patent_number=?", (cache_key,)).fetchone()
        if row:
            return json.loads(row[0])

    # Configure API endpoints and fields based on patent type
    if patent_type == "Patent Application":
        # For patent applications, use different endpoints and fields
        if field_type == "publication_number":
            search_queries = [
                {"q": f"publication_number:{normalized_number}"},
                {"q": {"publication_number": normalized_number}},
                {"q": {"_eq": {"publication_number": normalized_number}}},
            ]
            fields = ["publication_number", "patent_title", "patent_abstract", "publication_date", 
                     "application_number", "app_date", "assignee_organization", "inventor_name_first", "inventor_name_last"]
        else:
            search_queries = [
                {"q": f"application_number:{normalized_number}"},
                {"q": {"application_number": normalized_number}},
                {"q": {"_eq": {"application_number": normalized_number}}},
            ]
            fields = ["application_number", "patent_title", "patent_abstract", "publication_date", 
                     "publication_number", "app_date", "assignee_organization", "inventor_name_first", "inventor_name_last"]
    else:
        # For granted patents
        search_queries = [
            {"q": f"patent_id:{normalized_number}"},
            {"q": {"patent_number": normalized_number}},
            {"q": {"_eq": {"patent_number": normalized_number}}},
            {"q": {"patent_id": normalized_number}},
        ]
        fields = ["patent_id", "patent_number", "patent_title", "patent_abstract", "patent_date", 
                 "application_number", "app_date", "assignee_organization", "inventor_name_first", "inventor_name_last"]

    # Try new PatentSearch API first
    try:
        for query in search_queries:
            if isinstance(query["q"], str):
                search_query = {
                    "q": query["q"],
                    "fl": fields,
                    "sort": [{"patent_date": "desc"}] if patent_type == "Granted Patent" else [{"publication_date": "desc"}]
                }
            else:
                continue  # Skip dict queries for new API
            
            print(f"Trying PatentSearch API: {search_query}")
            response = requests.post(SEARCH_URL, json=search_query, timeout=10)
            print(f"PatentSearch API Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"PatentSearch API Response: {json.dumps(data, indent=2)[:500]}...")
                
                if "patents" in data and data["patents"]:
                    # Normalize data format
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
                    
    except Exception as e:
        print(f"PatentSearch API failed: {e}")

    # Fallback to legacy API
    for query in search_queries:
        if isinstance(query["q"], str):
            continue  # Skip string queries for legacy API
            
        query["f"] = [f.replace("patent_id", "patent_number") for f in fields]  # Legacy API uses different field names
        
        try:
            print(f"Trying legacy API: {json.dumps(query, indent=2)}")
            
            # Validate query structure before sending
            if not isinstance(query.get("q"), dict):
                print(f"Invalid query structure, skipping: {query}")
                continue
                
            response = requests.post(LEGACY_URL, json=query, timeout=10)
            print(f"Legacy API Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"Legacy API Response: {json.dumps(data, indent=2)[:300]}...")
                
                if "patents" in data and data["patents"]:
                    # Add patent type to data
                    for patent in data["patents"]:
                        patent["patent_type"] = patent_type
                    
                    with sqlite3.connect(DB_FILE) as conn:
                        conn.execute("REPLACE INTO patent_cache (patent_number, data_json) VALUES (?, ?)",
                                     (cache_key, json.dumps(data)))
                    return data
            else:
                print(f"Legacy API Error: {response.status_code}, {response.text[:200]}")
                
        except requests.exceptions.RequestException as e:
            print(f"Legacy API RequestException: {e}")
            continue
        except Exception as e:
            print(f"Legacy API Unexpected Exception: {e}")
            continue
    
    print(f"All API attempts failed for {patent_type} {normalized_number}")
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

# Patent type selection and input
patent_type = st.selectbox(
    "Select patent type:",
    ["Granted Patent", "Patent Application"],
    help="Granted patents have numbers like 6172354. Applications have numbers like 20230123456 or 16/123,456"
)

col1, col2 = st.columns([3, 1])
with col1:
    if patent_type == "Granted Patent":
        patent_input = st.text_input(
            "Enter US Patent Number:", 
            placeholder="e.g., 6172354, 11234567",
            help="Enter just the number without 'US' prefix or publication codes"
        )
    else:
        patent_input = st.text_input(
            "Enter US Patent Application Number:", 
            placeholder="e.g., 20230123456, 16123456, or 16/123,456",
            help="Published applications (20YYXXXXXXX) or filing numbers (XX/XXX,XXX or XXXXXXXX)"
        )
with col2:
    test_number = "6172354" if patent_type == "Granted Patent" else "20200123456"
    if st.button(f"Test with {test_number}"):
        patent_input = test_number

# Debug info toggle
show_debug = st.checkbox("Show debug information")

if patent_input:
    with st.spinner("Fetching and analyzing patent..."):
        patent_data = query_patent(patent_input.strip(), patent_type)
        
        if not patent_data:
            st.error("❌ Patent not found or API error.")
            
            with st.expander("🔍 Troubleshooting Tips"):
                if patent_type == "Granted Patent":
                    st.write("""
                    **For Granted Patents:**
                    - Ensure the patent number is correct (e.g., 6172354, not US6172354B1)
                    - Try removing any prefixes like "US" or suffixes like "B1"
                    - Some very old patents might not be in the database
                    
                    **Test Patents that should work:**
                    - 6172354 (Operator input device)
                    - 7654321, 8123456, 10123456
                    """)
                else:
                    st.write("""
                    **For Patent Applications:**
                    - Published applications: use format like 20230123456 (11 digits starting with 20)
                    - Application numbers: use format like 16123456 (7+ digits)
                    - You can use slashes like 16/123,456 - they'll be normalized
                    
                    **Test Applications that should work:**
                    - 20200123456 (published application)
                    - 16123456 (application number)
                    """)
                
            if show_debug:
                st.code("Debug: All API endpoints attempted. Check console/logs for detailed error messages.")
        else:
            patent = patent_data['patents'][0]
            
            st.subheader("📄 Patent/Application Metadata")
            
            # Display basic patent info with type-specific fields
            col1, col2 = st.columns(2)
            with col1:
                st.write("**Title:**", patent.get("patent_title", "N/A"))
                
                # Show appropriate number based on type
                if patent.get("patent_type") == "Patent Application":
                    if patent.get("publication_number"):
                        st.write("**Publication Number:**", patent.get("publication_number", "N/A"))
                    st.write("**Application Number:**", patent.get("application_number", "N/A"))
                else:
                    st.write("**Patent Number:**", patent.get("patent_number", "N/A"))
                    if patent.get("application_number"):
                        st.write("**Application Number:**", patent.get("application_number", "N/A"))
                
                st.write("**Filing Date:**", patent.get("filing_date", "N/A"))
                
                # Show appropriate publication/grant date
                if patent.get("patent_type") == "Patent Application":
                    st.write("**Publication Date:**", patent.get("patent_date", "N/A"))
                else:
                    st.write("**Grant Date:**", patent.get("patent_date", "N/A"))
            
            with col2:
                st.write("**Type:**", patent.get("patent_type", "Granted Patent"))
                
                assignees = patent.get("assignee_organization", [])
                if isinstance(assignees, list):
                    st.write("**Assignee(s):**", ", ".join(assignees) if assignees else "N/A")
                else:
                    st.write("**Assignee:**", assignees or "N/A")
                
                # Show inventors
                inventors = []
                first_names = patent.get("inventor_first_name", [])
                last_names = patent.get("inventor_last_name", [])
                
                if isinstance(first_names, list) and isinstance(last_names, list):
                    inventors = [f"{f} {l}" for f, l in zip(first_names, last_names) if f and l]
                elif first_names and last_names:
                    inventors = [f"{first_names} {last_names}"]
                
                st.write("**Inventor(s):**", ", ".join(inventors) if inventors else "N/A")
            
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

# Debug section (only show when debug is enabled)
if show_debug:
    st.subheader("🔧 Debug Tools")
    if st.button("Test API Connection"):
        try:
            # Simple test to see if APIs are reachable
            test_response = requests.get("https://api.patentsview.org", timeout=5)
            st.success(f"PatentsView API reachable. Status: {test_response.status_code}")
        except Exception as e:
            st.error(f"API connection test failed: {e}")
        
        try:
            # Test the normalize function
            test_normalized = normalize_patent_number("US6172354B1", "Granted Patent")
            st.info(f"Number normalization test: 'US6172354B1' → {test_normalized}")
        except Exception as e:
            st.error(f"Normalization test failed: {e}")
