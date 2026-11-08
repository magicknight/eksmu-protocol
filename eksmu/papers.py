"""Optional, declarative publication metadata. No rendering, I/O or execution."""
from datetime import date

from .store import Problem, canonical, exact, text

FORMAT = 'eksmu.paper-metadata.v1'
BUNDLE_FORMAT = 'eksmu.publication-bundle.v1'
MAX_BYTES = 48 * 1024


def validate_paper(value):
    exact(value, {'format', 'abstract', 'authors', 'references', 'license', 'provenance'})
    if value['format'] != FORMAT:
        raise Problem('Unsupported paper metadata format')
    text(value['abstract'], 6000, empty=True)
    text(value['license'], 4000, empty=True)
    authors = value['authors']
    if not isinstance(authors, list) or len(authors) > 32:
        raise Problem('最多列出32位作者；作者声明不会授予账户权限')
    for author in authors:
        exact(author, {'name', 'contribution'})
        text(author['name'], 160)
        if any(c in author['name'] for c in '\r\n'):
            raise Problem('作者署名需要写在一行')
        text(author['contribution'], 2000, empty=True)
    references = value['references']
    if not isinstance(references, list) or len(references) > 100:
        raise Problem('最多列出100条参考文献')
    for reference in references:
        text(reference, 2000)
        if any(c in reference for c in '\r\n'):
            raise Problem('每条参考文献需要写在一行')
    provenance = value['provenance']
    exact(provenance, {'source', 'prior_publication_date'})
    text(provenance['source'], 2000, empty=True)
    stamp = provenance['prior_publication_date']
    text(stamp, 10, empty=True)
    if stamp:
        try:
            if date.fromisoformat(stamp).isoformat() != stamp or not provenance['source'].strip():
                raise ValueError
        except ValueError as exc:
            raise Problem('既往发表日期需要YYYY-MM-DD及来源说明；日期仅为作者声明') from exc
    if len(canonical(value).encode('utf-8')) > MAX_BYTES:
        raise Problem('论文元数据合计最多48 KiB', 413)
