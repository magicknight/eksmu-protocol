# Eksmu Isolated LaTeX Compilation Container (Cloud Run Job)
# TeX Live 2026 has no TL2026-historic tag yet (Docker Hub, 2026-10-31), so the deployed image pins latest by digest.
FROM docker.io/texlive/texlive:latest@sha256:540d736c78e2803cdd7b6a196bd21339c30185948db55e04e75a415959102ca8

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3 ca-certificates fontconfig fonts-noto-cjk ghostscript poppler-utils imagemagick latexml; \
    rm -rf /var/lib/apt/lists/*; \
    for im_dir in /etc/ImageMagick-7 /etc/ImageMagick-6; do \
        if [ -d "$im_dir" ]; then \
            if [ -f "$im_dir/delegates.xml" ]; then \
                sed -i 's/-sDEVICE=pamcmyk32/-sDEVICE=png16m/g; s/-sDEVICE=pbmraw/-sDEVICE=pngalpha/g' "$im_dir/delegates.xml"; \
            fi; \
            printf '%s\n' \
              '<?xml version="1.0" encoding="UTF-8"?>' \
              '<!DOCTYPE policymap [' \
              '  <!ELEMENT policymap (policy)*>' \
              '  <!ATTLIST policymap xmlns CDATA #FIXED "">' \
              '  <!ELEMENT policy EMPTY>' \
              '  <!ATTLIST policy xmlns CDATA #FIXED "" domain NMTOKEN #REQUIRED name NMTOKEN #IMPLIED pattern CDATA #IMPLIED rights NMTOKEN #IMPLIED stealth NMTOKEN #IMPLIED value CDATA #IMPLIED>' \
              ']>' \
              '<policymap>' \
              '  <policy domain="Undefined" rights="none"/>' \
              '  <policy domain="resource" name="thread" value="1"/>' \
              '  <policy domain="resource" name="memory" value="512MiB"/>' \
              '  <policy domain="resource" name="map" value="1024MiB"/>' \
              '  <policy domain="resource" name="area" value="128MP"/>' \
              '  <policy domain="resource" name="disk" value="1GiB"/>' \
              '  <policy domain="resource" name="width" value="16KP"/>' \
              '  <policy domain="resource" name="height" value="16KP"/>' \
              '  <policy domain="resource" name="list-length" value="32"/>' \
              '  <policy domain="delegate" rights="none" pattern="*"/>' \
              '  <policy domain="delegate" rights="execute|read|write" pattern="{gs,ps:alpha,ps:color,ps:mono,ps:cmyk,pdf,ps,eps}"/>' \
              '  <policy domain="filter" rights="none" pattern="*"/>' \
              '  <policy domain="module" rights="none" pattern="*"/>' \
              '  <policy domain="module" rights="read|write" pattern="{PDF,PNG,SVG}"/>' \
              '  <policy domain="coder" rights="none" pattern="*"/>' \
              '  <policy domain="coder" rights="read|write" pattern="{PDF,PNG,SVG}"/>' \
              '  <policy domain="path" rights="none" pattern="@*"/>' \
              '  <policy domain="path" rights="none" pattern="-"/>' \
              '  <policy domain="path" rights="none" pattern="http*"/>' \
              '  <policy domain="path" rights="none" pattern="https*"/>' \
              '  <policy domain="path" rights="none" pattern="ftp*"/>' \
              '  <policy domain="path" rights="none" pattern="file*"/>' \
              '  <policy domain="path" rights="none" pattern="/etc/*"/>' \
              '  <policy domain="path" rights="none" pattern="/proc/*"/>' \
              '  <policy domain="path" rights="none" pattern="/sys/*"/>' \
              '  <policy domain="path" rights="none" pattern="/home/*"/>' \
              '  <policy domain="path" rights="none" pattern="/root/*"/>' \
              '  <policy domain="path" rights="none" pattern="/var/*"/>' \
              '  <policy domain="system" name="svg" rights="none" pattern="substitute-entities"/>' \
              '</policymap>' > "$im_dir/policy.xml"; \
        fi; \
    done; \
    groupadd --gid 10001 texrunner; \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /home/texrunner --shell /usr/sbin/nologin texrunner; \
    for npx in /usr/local/texlive/*/texmf-dist/tex/latex/newpx/newpxmath.sty; do \
        if [ -f "$npx" ]; then \
            sed -i 's/\\NewCommandCopy{\\mathscr}{\\mathScr}/\\DeclareCommandCopy{\\mathscr}{\\mathScr}/g' "$npx"; \
        fi; \
    done; \
    if [ -d /usr/share/texmf/tex/latex/latexml ]; then \
        mkdir -p /usr/local/texlive/texmf-local/tex/latex; \
        cp -r /usr/share/texmf/tex/latex/latexml /usr/local/texlive/texmf-local/tex/latex/; \
    fi; \
    LTXML_PKG_DIR="/usr/share/perl5/LaTeXML/Package"; \
    if [ -d "$LTXML_PKG_DIR" ]; then \
        if [ ! -f "$LTXML_PKG_DIR/xeCJK.sty.ltxml" ]; then \
            printf '%s\n' \
              'package LaTeXML::Package::Pool;' \
              'use strict; use warnings; use LaTeXML::Package;' \
              'DeclareOption(undef, undef); ProcessOptions();' \
              'RequirePackage("fontspec");' \
              'DefMacro("\\setCJKmainfont[]{}[]", "");' \
              'DefMacro("\\setCJKsansfont[]{}[]", "");' \
              'DefMacro("\\setCJKmonofont[]{}[]", "");' \
              'DefMacro("\\setCJKfamilyfont{}[]{}[]", "");' \
              'DefMacro("\\newCJKfontfamily[]{}[]{}", "");' \
              'DefMacro("\\xeCJKsetup{}", "");' \
              'DefMacro("\\defaultCJKfontfeatures{}", "");' \
              'DefMacro("\\addCJKfontfeatures{}", "");' \
              'DefMacro("\\CJKfamily{}", "");' \
              '1;' > "$LTXML_PKG_DIR/xeCJK.sty.ltxml"; \
            cp "$LTXML_PKG_DIR/xeCJK.sty.ltxml" "$LTXML_PKG_DIR/xecjk.sty.ltxml"; \
        fi; \
        if [ ! -f "$LTXML_PKG_DIR/ctex.sty.ltxml" ]; then \
            printf '%s\n' \
              'package LaTeXML::Package::Pool;' \
              'use strict; use warnings; use LaTeXML::Package;' \
              'DeclareOption(undef, undef); ProcessOptions();' \
              'RequirePackage("fontspec"); RequirePackage("xeCJK");' \
              'DefMacro("\\ctexset{}", "");' \
              'DefMacro("\\zihao{}", "");' \
              'DefMacro("\\songti", ""); DefMacro("\\heiti", "");' \
              'DefMacro("\\fangsong", ""); DefMacro("\\kaishu", "");' \
              'DefMacro("\\lishu", ""); DefMacro("\\youyuan", "");' \
              '1;' > "$LTXML_PKG_DIR/ctex.sty.ltxml"; \
        fi; \
        for pair in "ctexart:article" "ctexrep:report" "ctexbook:book" "ctexbeamer:beamer"; do \
            cls="${pair%%:*}"; base="${pair#*:}"; \
            if [ ! -f "$LTXML_PKG_DIR/${cls}.cls.ltxml" ]; then \
                printf '%s\n' \
                  'package LaTeXML::Package::Pool;' \
                  'use strict; use warnings; use LaTeXML::Package;' \
                  'DeclareOption(undef, undef); ProcessOptions();' \
                  "LoadClass(\"${base}\"); RequirePackage(\"ctex\");" \
                  '1;' > "$LTXML_PKG_DIR/${cls}.cls.ltxml"; \
            fi; \
        done; \
        if [ ! -f "$LTXML_PKG_DIR/unicode-math.sty.ltxml" ]; then \
            printf '%s\n' \
              'package LaTeXML::Package::Pool;' \
              'use strict; use warnings; use LaTeXML::Package;' \
              'DeclareOption(undef, undef); ProcessOptions();' \
              'RequirePackage("fontspec"); RequirePackage("amsmath");' \
              'DefMacro("\\setmathfont[]{}", "");' \
              'DefMacro("\\unimathsetup{}", "");' \
              '1;' > "$LTXML_PKG_DIR/unicode-math.sty.ltxml"; \
        fi; \
        if [ ! -f "$LTXML_PKG_DIR/luatexja.sty.ltxml" ]; then \
            printf '%s\n' \
              'package LaTeXML::Package::Pool;' \
              'use strict; use warnings; use LaTeXML::Package;' \
              'DeclareOption(undef, undef); ProcessOptions();' \
              'RequirePackage("fontspec");' \
              'DefMacro("\\setmainjfont[]{}", "");' \
              'DefMacro("\\setsansjfont[]{}", "");' \
              'DefMacro("\\setmonojfont[]{}", "");' \
              '1;' > "$LTXML_PKG_DIR/luatexja.sty.ltxml"; \
            cp "$LTXML_PKG_DIR/luatexja.sty.ltxml" "$LTXML_PKG_DIR/luatexja-fontspec.sty.ltxml"; \
        fi; \
    fi; \
    for fc_conf in /usr/local/texlive/*/texmf-var/fonts/conf/texlive-fontconfig.conf; do \
        if [ -f "$fc_conf" ]; then \
            cp "$fc_conf" /etc/fonts/conf.d/09-texlive-fonts.conf; \
        fi; \
    done; \
    mktexlsr; \
    fc-cache -fv; \
    chmod -R a+rX /var/cache/fontconfig; \
    luaotfload-tool --update --force; \
    mkdir -p /var/cache/eksmu-texmfvar /tmp/tex-warmup; \
    TEXMFVAR=/var/cache/eksmu-texmfvar TEXMFCACHE=/var/cache/eksmu-texmfvar luaotfload-tool --update --force; \
    printf '%s\n' \
      '\documentclass{ctexart}' \
      '\usepackage{fontspec}' \
      '\setmainfont{Latin Modern Roman}' \
      '\newfontfamily{\cjkfont}{Noto Serif CJK SC}' \
      '\newfontfamily{\cjkkai}{AR PL KaitiM Big5}' \
      '\begin{document}' \
      '你好，世界！Hello World $E=mc^2$.' \
      '{\cjkfont 思源宋体\textbf{粗体}}' \
      '{\cjkkai 文鼎中楷}' \
      '{\fontspec{TeX Gyre Termes}Termes}' \
      '{\fontspec{TeX Gyre Heros}Heros}' \
      '\end{document}' > /tmp/tex-warmup/warmup.tex; \
    (cd /tmp/tex-warmup && HOME=/tmp/tex-warmup TEXMFVAR=/var/cache/eksmu-texmfvar TEXMFCACHE=/var/cache/eksmu-texmfvar xelatex -interaction=nonstopmode warmup.tex); \
    (cd /tmp/tex-warmup && HOME=/tmp/tex-warmup TEXMFVAR=/var/cache/eksmu-texmfvar TEXMFCACHE=/var/cache/eksmu-texmfvar lualatex -interaction=nonstopmode warmup.tex); \
    (cd /tmp/tex-warmup && HOME=/tmp/tex-warmup TMPDIR=/tmp/tex-warmup MAGICK_THREAD_LIMIT=1 OMP_NUM_THREADS=1 perl -MImage::Magick -e 'my $im = Image::Magick->new(); my $err = $im->Read("/tmp/tex-warmup/warmup.pdf"); die "IM Read PDF failed: $err" if $err; $err = $im->Write("/tmp/tex-warmup/warmup.png"); die "IM Write PNG failed: $err" if $err;' && test -s /tmp/tex-warmup/warmup.png); \
    rm -rf /tmp/tex-warmup; \
    chmod -R a+rX /var/cache/eksmu-texmfvar

WORKDIR /app
COPY compile_entry.py /app/compile_entry.py
RUN chmod 0555 /app/compile_entry.py

USER 10001:10001
ENTRYPOINT ["python3", "/app/compile_entry.py"]
