package android.database;
public interface Cursor {
    int getCount(); boolean moveToFirst(); boolean moveToNext(); boolean isAfterLast();
    String getString(int i); long getLong(int i); int getInt(int i); byte[] getBlob(int i);
    int getColumnIndex(String n); void close();
}
