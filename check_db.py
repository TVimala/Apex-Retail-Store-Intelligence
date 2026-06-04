import sqlite3

conn = sqlite3.connect("data/store_intelligence.db")
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
print(cur.fetchall())

cur.execute("SELECT COUNT(*) FROM events;")
print(cur.fetchall())

conn.close()