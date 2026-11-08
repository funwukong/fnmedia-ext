package android.database.sqlite;
public class EmptyCursor implements android.database.Cursor {
    public int getCount(){return 0;} public boolean moveToFirst(){return false;}
    public boolean moveToNext(){return false;} public boolean isAfterLast(){return true;}
    public String getString(int i){return null;} public long getLong(int i){return 0;}
    public int getInt(int i){return 0;} public byte[] getBlob(int i){return null;}
    public int getColumnIndex(String n){return -1;} public void close(){}
}
