package android.database.sqlite;
import android.content.ContentValuesDummy;
import android.database.Cursor;
public class SQLiteDatabase extends SQLiteClosable {
    public interface CursorFactory { android.database.Cursor newCursor(SQLiteDatabase db); }
    public static SQLiteDatabase openOrCreateDatabase(String path, CursorFactory f){ return new SQLiteDatabase(); }
    public static SQLiteDatabase openOrCreateDatabase(java.io.File f, CursorFactory cf){ return new SQLiteDatabase(); }
    public void execSQL(String sql){}
    public android.database.Cursor rawQuery(String sql, String[] args){ return new EmptyCursor(); }
    public long insert(String t, String nullCol, android.content.ContentValuesDummy v){ return -1; }
    public int delete(String t,String w,String[] a){ return 0; }
    public int update(String t, android.content.ContentValuesDummy v, String w, String[] a){ return 0; }
    public void beginTransaction(){}
    public void setTransactionSuccessful(){}
    public void endTransaction(){}
}
