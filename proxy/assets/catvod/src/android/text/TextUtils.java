package android.text;
public class TextUtils {
    public static boolean isEmpty(CharSequence s){ return s==null || s.length()==0; }
    public static boolean equals(CharSequence a, CharSequence b){
        if (a==b) return true; if (a==null||b==null) return false; return a.toString().equals(b.toString());
    }
    public static String join(CharSequence sep, Object[] arr){
        StringBuilder sb=new StringBuilder(); for(int i=0;i<arr.length;i++){ if(i>0)sb.append(sep); sb.append(arr[i]); } return sb.toString();
    }
    public static String join(CharSequence sep, Iterable<?> arr){
        StringBuilder sb=new StringBuilder(); boolean f=true; for(Object o:arr){ if(!f)sb.append(sep); f=false; sb.append(o); } return sb.toString();
    }
}
