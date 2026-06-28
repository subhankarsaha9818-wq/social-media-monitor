import os
import json
import datetime
from mcp.server.fastmcp import FastMCP

# Instantiate FastMCP server
mcp = FastMCP("social-media-monitor-mcp-server")

# Base directory for data files
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Ensure default files exist
GUIDELINES_PATH = os.path.join(DATA_DIR, "brand_guidelines.txt")
if not os.path.exists(GUIDELINES_PATH):
    with open(GUIDELINES_PATH, "w", encoding="utf-8") as f:
        f.write("""Brand Voice & Tone Guidelines:
- Professional, empathetic, and constructive.
- Always apologize if the customer had a negative experience, and offer a practical solution or escalation path.
- Keep responses under 280 characters to fit standard social media constraints.
- Never argue, blame, or dismiss customer complaints.
""")

TEMPLATES_PATH = os.path.join(DATA_DIR, "response_templates.json")
if not os.path.exists(TEMPLATES_PATH):
    default_templates = [
        {"keyword": "refund", "template": "We are sorry for the billing issue. Please DM us your account details so we can process your refund immediately."},
        {"keyword": "quality", "template": "We strive for excellence and apologize that we fell short. Let's make this right—please DM us to coordinate."},
        {"keyword": "thank you", "template": "Thank you so much for the kind words! We are thrilled to hear you had a great experience."}
    ]
    with open(TEMPLATES_PATH, "w", encoding="utf-8") as f:
        json.dump(default_templates, f, indent=2)

INCIDENTS_PATH = os.path.join(DATA_DIR, "incidents.json")
if not os.path.exists(INCIDENTS_PATH):
    with open(INCIDENTS_PATH, "w", encoding="utf-8") as f:
        json.dump([], f)

@mcp.tool()
def fetch_brand_guidelines() -> str:
    """Fetches the company's social media brand voice and tone guidelines.

    Returns:
        The content of the brand guidelines document.
    """
    try:
        with open(GUIDELINES_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading guidelines: {str(e)}"

@mcp.tool()
def search_response_templates(query: str) -> str:
    """Searches for approved response templates matching the query/keyword.

    Args:
        query: The keyword or phrase to search templates for (e.g., 'refund', 'thank you').

    Returns:
        A list of matching response templates.
    """
    try:
        with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
            templates = json.load(f)
        matches = [t["template"] for t in templates if query.lower() in t["keyword"].lower() or query.lower() in t["template"].lower()]
        if not matches:
            return "No matching templates found. Draft a customized response using brand guidelines."
        return "\n".join(matches)
    except Exception as e:
        return f"Error searching templates: {str(e)}"

@mcp.tool()
def log_incident(post_content: str, sentiment: str, suggested_response: str) -> str:
    """Logs a social media incident or negative sentiment post for internal tracking and follow-up.

    Args:
        post_content: The original social media post content.
        sentiment: The analyzed sentiment of the post.
        suggested_response: The draft response that was generated.

    Returns:
        A status message indicating success or failure.
    """
    try:
        with open(INCIDENTS_PATH, "r", encoding="utf-8") as f:
            incidents = json.load(f)
        
        new_incident = {
            "id": len(incidents) + 1,
            "post_content": post_content,
            "sentiment": sentiment,
            "suggested_response": suggested_response,
            "status": "Logged",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z"
        }
        incidents.append(new_incident)
        
        with open(INCIDENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(incidents, f, indent=2)
        
        return f"Incident logged successfully with ID {new_incident['id']}."
    except Exception as e:
        return f"Error logging incident: {str(e)}"

if __name__ == "__main__":
    mcp.run()
