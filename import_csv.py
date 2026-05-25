import pandas as pd
import sqlite3

df = pd.read_csv('/mnt/c/Users/max_j/Downloads/hyrox_results.csv/hyrox_results.csv', encoding='utf-8')

conn = sqlite3.connect('hyrox.db')
df.to_sql('results', conn, if_exists='replace', index=False)
conn.close()

print("Done. Rows imported:", len(df))
