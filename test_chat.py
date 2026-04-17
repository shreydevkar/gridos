import requests

API_URL = "http://127.0.0.1:8000"

def test_ai_agent():
    # --- STEP 1: SEED THE DATA AND CHECK STATUS ---
    print("📥 Seeding Revenue (1000) and Expenses (400)...")
    seed_payload = {
        "agent_id": "User-Setup",
        "target_start_a1": "C3",
        "data_payload": [[1000, 400]], 
        "shift_direction": "right"
    }
    seed_res = requests.post(f"{API_URL}/agent/write", json=seed_payload)
    seed_data = seed_res.json()
    print(f"📡 Seed Status: {seed_data.get('status')} | Target: {seed_data.get('actual_target')}")

    # --- STEP 2: ASK THE AI TO CALCULATE ---
    print("\n🚀 Sending request to GridOS AI...")
    chat_payload = {
        "prompt": "Use C3 and D3 to calculate Net Profit. Put the result in E3.",
        "history": []
    }

    response = requests.post(f"{API_URL}/agent/chat", json=chat_payload)
    
    if response.status_code == 200:
        data = response.json()
        print(f"🤖 ROUTER: {data['category'].upper()}")
        print(f"📝 AI SUGGESTED: {data['values_written']}")
        print(f"📍 FINAL PLACEMENT: {data['final_placement']}")
        
        print("\n--- FINAL GRID STATE ---")
        grid = requests.get(f"{API_URL}/debug/grid").json()
        
        # We check all three cells to see what the Kernel actually holds
        for cell in ["C3", "D3", "E3"]:
            state = grid.get(cell)
            if state:
                print(f"✅ {cell}: {state['value']} (Formula: {state['formula']})")
            else:
                print(f"❌ {cell}: EMPTY/NOT FOUND")
    else:
        print(f"❌ Error: {response.text}")

if __name__ == "__main__":
    test_ai_agent()