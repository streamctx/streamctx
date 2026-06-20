
import time
import sqlite3

def run_real_database_simulation():
    print("🚀 Running Backend Agent and Logging to Database...")

    # SQLite ડેટાબેઝ કનેક્શન (તમારા storage.py જેવું જ લોજિક)
    conn = sqlite3.connect("streamctx_live.db")
    cursor = conn.cursor()

    # નવું ટેબલ બનાવવું
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS live_stats (
            step_id INTEGER PRIMARY KEY AUTOINCREMENT,
            step_name TEXT,
            normal_tokens INTEGER,
            streamctx_tokens INTEGER,
            poison_blocked INTEGER,
            self_healed INTEGER
        )
    """)
    # જૂનો ડેટા સાફ કરવો જેથી ફ્રેશ રન થાય
    cursor.execute("DELETE FROM live_stats")
    conn.commit()

    steps = [
        ("Step 1: Scraping", 17, 6, 0, 0),
        ("Step 2: Poison Test", 33, 6, 1, 0),
        ("Step 3: Context-Diff", 50, 6, 1, 0),
        ("Step 4: Self-Healing", 66, 6, 1, 1),
        ("Step 5: Report", 83, 6, 1, 1)
    ]

    for name, normal, ctx, poison, heal in steps:
        print(f"Executing {name}...")
        time.sleep(3) # ડેશબોર્ડ પર લાઈવ અસર જોવા માટે ગેપ રાખ્યો છે

        # ડેટાબેઝમાં લાઈવ ઇન્સર્ટ કરવું
        cursor.execute("""
            INSERT INTO live_stats (step_name, normal_tokens, streamctx_tokens, poison_blocked, self_healed)
            VALUES (?, ?, ?, ?, ?)
        """, (name, normal, ctx, poison, heal))
        conn.commit()

    conn.close()
    print("🎉 Backend Task Finished!")

if __name__ == "__main__":
    run_real_database_simulation()




