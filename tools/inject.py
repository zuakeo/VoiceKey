import sys, zipfile, shutil, os

src_apk, dex_path, out_apk = sys.argv[1], sys.argv[2], sys.argv[3]

shutil.copyfile(src_apk, out_apk)
with zipfile.ZipFile(out_apk, 'a', zipfile.ZIP_DEFLATED) as z:
    z.write(dex_path, 'classes.dex')

print("injected %s -> %s" % (os.path.basename(dex_path), os.path.basename(out_apk)))
