"""User-provided source adapters.

Drop a `*.py` module here that defines a subclass of `sources.base.MangaSource`
and it will be auto-discovered at startup (see sources/registry.py). Everything in
this folder except this file is git-ignored, so your own adapters stay local and
never enter the repository.

Example (my_source.py):

    from ..base import MangaSource, SeriesMeta, ChapterMeta

    class MySource(MangaSource):
        name = "mysite"
        label = "My Site"
        def matches(self, url): ...
        def fetch_series(self, url): ...
        def fetch_pages(self, chapter): ...
"""
