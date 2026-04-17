import requests

API_URL = "http://127.0.0.1:8000"

def run_test():
    print("🤖 Simulating Agent 'Finance-Bot' using CELL REFERENCES...")
    
    intent_payload = {
        "agent_id": "Finance-Bot",
        "target_start_a1": "A2", 
        "data_payload": [
            ["Rev", "Cost", "Total Rev"],
            [150, 50, "=SUM(C3, D3)"],    # <-- Now using C3 and D3!
            [200, 80, "=MAX(C4, D4)"],    # <-- Now using C4 and D4!
            [0, 0, "=MAGIC(C5, D5)"]      # <-- Unknown formula
        ],
        "shift_direction": "right"
    }

    requests.post(f"{API_URL}/agent/write", json=intent_payload)
    
    print("\n--- CURRENT GRID STATE (Occupied Cells) ---")
    grid = requests.get(f"{API_URL}/debug/grid").json()
    for cell, state in grid.items():
        if state['locked']:
            pass # Skipping locked prints to keep the console clean
        else:
            if state['formula']:
                print(f"✅ {cell}: {state['value']} (Formula: {state['formula']})")
            else:
                print(f"✅ {cell}: {state['value']}")

if __name__ == "__main__":
    run_test()