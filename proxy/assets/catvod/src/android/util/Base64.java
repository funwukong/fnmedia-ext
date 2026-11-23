package android.util;
public class Base64 {
    public static final int DEFAULT=0, NO_PADDING=1, NO_WRAP=2, URL_SAFE=8;
    public static byte[] decode(String s, int flags){ return java.util.Base64.getDecoder().decode(s.replaceAll("\\s","")); }
    public static byte[] decode(byte[] b, int flags){ return java.util.Base64.getDecoder().decode(b); }
    public static String encodeToString(byte[] b, int flags){ return java.util.Base64.getEncoder().encodeToString(b); }
    public static byte[] encode(byte[] b, int flags){ return java.util.Base64.getEncoder().encode(b); }
}
