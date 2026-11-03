"""Daily deletion of expired AI outputs; does not reset durable usage counters."""
from datetime import datetime, timedelta
from ai_features import RETENTION_DAYS, TZ
from server import Database

def main():
    database=Database(initialize=False)
    cutoff=(datetime.now(TZ)-timedelta(days=RETENTION_DAYS)).isoformat(timespec='seconds')
    with database.connection() as conn:
        with database.transaction(conn):
            count=conn.execute("SELECT COUNT(*) AS count FROM ai_records WHERE kind='run' AND created_at<?",(cutoff,)).fetchone()['count']
            conn.execute("DELETE FROM ai_records WHERE kind='run' AND created_at<?",(cutoff,))
    print(f'Expired AI results removed: {count}')

if __name__=='__main__':main()
