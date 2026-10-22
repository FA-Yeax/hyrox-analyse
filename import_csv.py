import argparse
import sqlite3

import pandas as pd

parser = argparse.ArgumentParser(description='Importiert HYROX-Ergebnisse aus einer CSV in die SQLite-Datenbank.')
parser.add_argument('csv_path', help='Pfad zur CSV-Datei mit den Ergebnissen')
parser.add_argument('--db', default='hyrox.db', help='Ziel-Datenbank (Standard: hyrox.db)')
args = parser.parse_args()

df = pd.read_csv(args.csv_path, encoding='utf-8')

conn = sqlite3.connect(args.db)
df.to_sql('results', conn, if_exists='replace', index=False)
conn.close()

print("Done. Rows imported:", len(df))
