# frozen_string_literal: true

# search.json indexes each page's rendered content, and Jekyll renders pages in site.pages order: a
# page after the index would still hold its Markdown, so search snippets showed "**" and "](". Moving
# the index to the end means every page it reads has been converted. (`markdownify` in the template
# can't stand in: it skips the alert escaping in github_markdown.rb, so kramdown warns on `[!TIP]`.)
Jekyll::Hooks.register :site, :pre_render do |site|
  index, pages = site.pages.partition { |page| page.relative_path == "search.json" }
  site.pages.replace(pages + index)
end
