import sqlite3
from database import get_history_detail

def view_history_by_username(username):
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    # Query untuk mengambil histori berdasarkan username
    cursor.execute('''
    SELECT history_detail.start_time, history_detail.stop_time, history_detail.drowsiness_type, 
           history_detail.drowsiness_time, history_detail.drowsiness_duration
    FROM history_detail
    JOIN users ON history_detail.user_id = users.id
    WHERE users.username = ?
    ''', (username,))
    
    history = cursor.fetchall()
    
    conn.close()
    
    if history:
        print(f"Histori untuk {username}:")
        for record in history:
            print(f"Start: {record[0]}, Stop: {record[1]}, "
                  f"Jenis Ngantuk: {record[2]}, Waktu: {record[3]}, Lama: {record[4]}")
    else:
        print(f"Tidak ada histori untuk {username}.")

def view_history_detail(username, start_time):
    conn = sqlite3.connect('user_data.db')
    cursor = conn.cursor()
    
    # Query untuk mengambil histori berdasarkan username dan start_time
    cursor.execute('''
    SELECT history_detail.start_time, history_detail.stop_time, history_detail.drowsiness_type, 
           history_detail.drowsiness_time, history_detail.drowsiness_duration
    FROM history_detail
    JOIN users ON history_detail.user_id = users.id
    WHERE users.username = ? AND history_detail.start_time = ?
    ''', (username, start_time))
    
    history = cursor.fetchall()
    
    conn.close()
    
    if history:
        print(f"Histori untuk {username}:")
        for record in history:
            print(f"Start: {record[0]}, Stop: {record[1]}, "
                  f"Jenis Ngantuk: {record[2]}, Waktu: {record[3]}, Lama: {record[4]}")
    else:
        print(f"Tidak ada histori untuk {username}.")


if __name__ == "__main__":
    # view_history_by_username("apriadi")
    view_history_detail("apriadi", "2024-12-28 08:30:00")