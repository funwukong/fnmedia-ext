package android.net.ssl;
import javax.net.ssl.SSLSocket;
public class SSLSockets {
    public static void setApplicationProtocols(SSLSocket s, String[] p){
        try { javax.net.ssl.SSLParameters sp=s.getSSLParameters(); if(p!=null){sp.setApplicationProtocols(p); s.setSSLParameters(sp);} } catch(Exception e){}
    }
    public static String[] getApplicationProtocols(SSLSocket s){ return new String[0]; }
}
