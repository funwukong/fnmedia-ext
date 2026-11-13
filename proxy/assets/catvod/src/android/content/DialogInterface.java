package android.content;
public interface DialogInterface {
    interface OnClickListener { void onClick(DialogInterface d, int which); }
    interface OnCancelListener { void onCancel(DialogInterface d); }
    void dismiss();
}
