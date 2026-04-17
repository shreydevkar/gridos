"""Quick live test for the /agent/chat/chain endpoint.

Run with the server already up:
    python test_chain.py
"""

import json
import requests

API_URL = "http://127.0.0.1:8000"

PROMPT = "Write =SUM(10, 20) in A3. Then write =MAX(A3, 100) in B3."

def main():
    payload = {"prompt": PROMPT, "scope": "sheet"}
    print(f"POST /agent/chat/chain  prompt={PROMPT!r}\n")

    res = requests.post(f"{API_URL}/agent/chat/chain", json=payload, timeout=60)
    print(f"Status: {res.status_code}")

    if not res.ok:
        print(res.text)
        return

    data = res.json()
    print(f"Sheet: {data.get('sheet')}")
    print(f"Iterations used: {data.get('iterations_used')}")
    print(f"Terminated early: {data.get('terminated_early')}")
    print()

    for step in data.get("steps", []):
        print(f"--- Iteration {step['iteration']} (agent: {step['agent_id']}) ---")
        print(f"  reasoning : {step.get('reasoning')}")
        print(f"  target    : {step.get('target')}")
        print(f"  values    : {step.get('values')}")
        for obs in step.get("observations", []):
            formula = f"  (formula: {obs['formula']})" if obs.get("formula") else ""
            print(f"  obs       : {obs['cell']} = {obs['value']}{formula}")
        print()


if __name__ == "__main__":
    main()
