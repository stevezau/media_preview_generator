# frozen_string_literal: true

require "pathname"

# Keeps the GitHub-flavoured Markdown in docs/ reading the same on the site as on github.com.
#
# 1. GitHub alerts (`> [!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]`, `[!CAUTION]`) become the
#    theme's .callout boxes. kramdown renders an alert as a plain blockquote whose first paragraph
#    starts with the literal marker, so this works on the converted HTML: find that blockquote, find
#    its matching close tag (blockquotes nest), and swap the wrapper. Before conversion the
#    marker's bracket is escaped in memory: kramdown otherwise reads `[!NOTE]` as a reference link
#    with no definition and warns once per alert, burying real warnings. The output is the same.
# 2. Relative src/href values are resolved from the page's own source folder into site paths.
#    Pretty permalinks put getting-started.md at /getting-started/, where a relative
#    "images/x.webp" would otherwise load /getting-started/images/x.webp. Links to docs/README.md
#    (the GitHub docs hub, excluded from the site) point at the site home instead.
# 3. Pictures from docs/images/ get width and height, so text doesn't jump as they load. Markdown
#    can't carry them: kramdown's `{: width="..."}` prints as text on github.com, and a raw
#    <img width height> stretches there (GitHub's CSS sets max-width but not height: auto). Every
#    picture there is a 2x capture, so the reserved size is half its pixels, as on the landing page.
#    A picture whose size can't be read (an SVG has none in pixels) is left as written: failing the
#    build would also stop a plugin release, which deploys these docs.
# 4. Tables get a .table-scroll box, so a wide one scrolls on its own on a phone instead of making the
#    whole page scroll sideways. Done here rather than in site.js so it holds with scripts blocked.
#
# The HTML work runs on :post_convert: after Markdown -> HTML, before the layout, so layouts are
# never touched.
module GithubMarkdown
  ALERT_START = %r{<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(?:<br\s*/?>)?\s*}
  ICONS = {
    "NOTE" => '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5M12 8h.01"/>',
    "TIP" => '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3Z"/>',
    "IMPORTANT" => '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"/><path d="M12 7v4M12 14h.01"/>',
    "WARNING" => '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>' \
                 '<path d="M12 9v4M12 17h.01"/>',
    "CAUTION" => '<path d="M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9Z"/><path d="M12 8v4M12 16h.01"/>'
  }.freeze
  # src/href values that are relative paths: no scheme, not site-absolute, not a same-page anchor.
  RELATIVE_ATTR = /\b(src|href)="(?![a-z][a-z0-9+.-]*:|\/|#)([^"]*)"/i
  CLOSE = "</blockquote>"
  # A blockquote line (at any nesting depth) that opens with an alert marker, in Markdown source.
  ALERT_MARKER = /^((?:[ \t]*>)+[ \t]*)\[(!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\])/
  # A fenced code block's opening or closing line; it closes on the same character, at least as long.
  FENCE = /^[ \t]*(`{3,}|~{3,})/
  IMG_TAG = /<img\b[^>]*>/
  TABLE = %r{<table\b.*?</table>}m
  PNG_SIGNATURE = "\x89PNG\r\n\x1a\n".b

  module_function

  def escape_alert_markers(markdown)
    fence = nil
    markdown.each_line.map do |line|
      marker = line[FENCE, 1]
      if fence
        fence = nil if marker && marker[0] == fence[0] && marker.length >= fence.length
        line
      elsif marker
        fence = marker
        line
      else
        line.sub(ALERT_MARKER) { "#{Regexp.last_match(1)}\\[#{Regexp.last_match(2)}" }
      end
    end.join
  end

  def convert_alerts(html)
    out = +""
    pos = 0
    while (match = ALERT_START.match(html, pos))
      close = matching_close(html, match.end(0))
      raise "GitHub alert without a closing </blockquote> near #{html[match.begin(0), 80].inspect}" unless close

      body = "<p>#{html[match.end(0)...close]}".sub(%r{\A<p>\s*</p>\s*}, "")
      out << html[pos...match.begin(0)] << callout(match[1], body)
      pos = close + CLOSE.length
    end
    out << html[pos..]
  end

  def matching_close(html, from)
    depth = 1
    cursor = from
    loop do
      next_close = html.index(CLOSE, cursor)
      return nil unless next_close

      next_open = html.index("<blockquote", cursor)
      if next_open && next_open < next_close
        depth += 1
        cursor = next_open + 1
      else
        depth -= 1
        return next_close if depth.zero?

        cursor = next_close + 1
      end
    end
  end

  def callout(kind, body)
    %(<div class="callout callout--#{kind.downcase}" role="note">) +
      %(<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">#{ICONS.fetch(kind)}</svg>) +
      %(<div><p class="callout__label">#{kind.capitalize}</p>#{body}</div></div>)
  end

  def absolutize(html, page_dir, baseurl)
    html.gsub(RELATIVE_ATTR) do
      attr = Regexp.last_match(1)
      target = Regexp.last_match(2)
      next Regexp.last_match(0) if target.empty?

      path, separator, rest = target.partition(/[?#]/)
      resolved = Pathname.new("/#{page_dir}").join(path).cleanpath.to_s
      resolved = "/" if resolved == "/README.md"
      %(#{attr}="#{baseurl}#{resolved}#{separator}#{rest}")
    end
  end

  # Runs after absolutize, so a docs picture's src is always "<baseurl>/images/<name>".
  def add_image_sizes(html, source_dir, baseurl)
    html.gsub(IMG_TAG) do |tag|
      src = tag[/\bsrc="([^"]+)"/, 1]
      next tag if src.nil? || !src.start_with?("#{baseurl}/images/") || tag.match?(/\bwidth=/)

      width, height = pixel_size(File.join(source_dir, src.delete_prefix(baseurl)))
      next tag unless width

      tag.sub(%r{\s*/?>\z}) { |close| %( width="#{width / 2}" height="#{height / 2}"#{close}) }
    end
  end

  def wrap_tables(html)
    html.gsub(TABLE) { |table| %(<div class="table-scroll">#{table}</div>) }
  end

  # [width, height] in pixels, read from the file header: PNG, or WebP in any of its three layouts.
  # nil for any other format.
  def pixel_size(path)
    head = File.binread(path, 30)
    if head.start_with?(PNG_SIGNATURE)
      head[16, 8].unpack("NN")
    elsif head[0, 4] == "RIFF" && head[8, 4] == "WEBP"
      case head[12, 4]
      when "VP8 " then head[26, 4].unpack("vv").map { |side| side & 0x3fff }
      when "VP8L"
        bits = head[21, 4].unpack1("V")
        [(bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1]
      when "VP8X" then [24, 27].map { |at| (head[at, 3] + "\0".b).unpack1("V") + 1 }
      end
    end
  end
end

Jekyll::Hooks.register :pages, :pre_render do |page|
  next unless page.ext == ".md"

  page.content = GithubMarkdown.escape_alert_markers(page.content)
end

Jekyll::Hooks.register :pages, :post_convert do |page|
  next unless page.ext == ".md"

  page_dir = File.dirname(page.relative_path)
  baseurl = page.site.baseurl.to_s
  html = GithubMarkdown.absolutize(GithubMarkdown.convert_alerts(page.content), page_dir, baseurl)
  page.content = GithubMarkdown.wrap_tables(GithubMarkdown.add_image_sizes(html, page.site.source, baseurl))
end
