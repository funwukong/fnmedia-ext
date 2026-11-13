package com.fnmedia.bridge;

import java.lang.reflect.Field;

/**
 * 在进程内把 android.os.Build / Build.VERSION 伪装成真机。
 *
 * 加固 native 库（ftyguard）没有 __system_property_get / open / ptrace 等导入，
 * 只依赖 libandroid(JNI) 与 liblog，其反模拟器检测在 JNI_OnLoad 内经 JNI 回调读
 * Java 侧 Build 字段（MODEL/HARDWARE/FINGERPRINT/PRODUCT/SUPPORTED_ABIS 等）。
 * 因此在 System.load 之前用反射改写本进程的 Build 静态字段，native 读到的即真机值，
 * 无需 root / resetprop / magisk。对 App 私有进程生效，不影响系统其它进程。
 */
public final class BuildSpoof {

    private static volatile boolean DONE = false;

    // 小米 10 (umi)，arm64 真机，Android 11
    public static synchronized void apply() {
        if (DONE) return;
        try {
            Class<?> b = android.os.Build.class;
            setS(b, "MANUFACTURER", "Xiaomi");
            setS(b, "BRAND", "Xiaomi");
            setS(b, "MODEL", "Mi 10");
            setS(b, "DEVICE", "umi");
            setS(b, "PRODUCT", "umi");
            setS(b, "BOARD", "kona");
            setS(b, "HARDWARE", "qcom");
            setS(b, "HOST", "pangu-build-component-system");
            setS(b, "BOOTLOADER", "unknown");
            setS(b, "FINGERPRINT",
                "Xiaomi/umi/umi:11/RKQ1.200826.002/V12.5.3.0.RJBCNXM:user/release-keys");
            setS(b, "TAGS", "release-keys");
            setS(b, "TYPE", "user");
            setS(b, "ID", "RKQ1.200826.002");
            setS(b, "DISPLAY", "RKQ1.200826.002");
            // SUPPORTED_ABIS 是 String[] final：反射拿到数组引用后改元素
            try {
                Field f = b.getDeclaredField("SUPPORTED_ABIS");
                f.setAccessible(true);
                String[] abis = (String[]) f.get(null);
                String[] want = {"arm64-v8a", "armeabi-v7a", "armeabi"};
                for (int i = 0; i < abis.length && i < want.length; i++) abis[i] = want[i];
            } catch (Throwable ignored) {}

            Class<?> v = android.os.Build.VERSION.class;
            setS(v, "RELEASE", "11");
            setS(v, "INCREMENTAL", "V12.5.3.0.RJBCNXM");
            try {
                Field sd = v.getDeclaredField("SDK_INT");
                sd.setAccessible(true); sd.setInt(null, 30);
            } catch (Throwable ignored) {}

            Class<?> rb = android.os.Build.VERSION_CODES.class; // no-op 引用，确保类存在
            DONE = true;
        } catch (Throwable t) {
            // 伪装失败不致命；记录但继续
            System.err.println("[BuildSpoof] partial: " + t);
            DONE = true;
        }
    }

    private static void setS(Class<?> c, String name, String val) {
        try {
            Field f = c.getDeclaredField(name);
            f.setAccessible(true);
            // 去除 final 修饰（旧版 ART 对 String 静态 final 缓存不敏感，反射可直接写）
            try {
                Field mf = Field.class.getDeclaredField("accessFlags");
                mf.setAccessible(true);
                mf.setInt(f, f.getModifiers() & ~java.lang.reflect.Modifier.FINAL);
            } catch (Throwable ignored) {
                // 新版 ART 没有 accessFlags 字段；final 静态非内联基础上 set 仍可能成功
            }
            f.set(null, val);
        } catch (Throwable ignored) {}
    }
}
