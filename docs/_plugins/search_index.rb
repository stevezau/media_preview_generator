# frozen_string_literal: true

require "cgi"

# search.json indexes each page's rendered content, and Jekyll renders pages in site.pages order: a
# page after the index would still hold its Markdown, so search snippets showed "**" and "](". Moving
# the index to the end means every page it reads has been converted. (`markdownify` in the template
# can't stand in: it skips the alert escaping in github_markdown.rb, so kramdown warns on `[!TIP]`.)
Jekyll::Hooks.register :site, :pre_render do |site|
  index, pages = site.pages.partition { |page| page.relative_path == "search.json" }
  site.pages.replace(pages + index)
end

Jekyll::Hooks.register :pages, :post_render do |page|
  next unless page.output_ext == ".html"

  sections = []
  matches = page.output.to_enum(:scan, /<h([23])\s+id="([^"]+)"[^>]*>(.*?)<\/h\1>/mi).map { Regexp.last_match }
  matches.each_with_index do |match, index|
    finish = matches[index + 1]&.begin(0) || page.output.length
    body = page.output[match.end(0)...finish]
    clean = lambda do |html|
      CGI.unescapeHTML(html.gsub(/<[^>]*>/, " ")).gsub(/\s+/, " ").strip
    end
    sections << {
      "title" => clean.call(match[3]).sub(/\s*#\z/, ""),
      "url" => "#{page.url}##{match[2]}",
      "content" => clean.call(body)[0, 700]
    }
  end
  page.data["search_sections"] = sections
end
