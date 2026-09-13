using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Mime;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Plugin.MediaPreviewBridge.Markers;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaSegments;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.MediaPreviewBridge.Api;

/// <summary>One segment in a markers push.</summary>
public class MarkerSegmentDto
{
    /// <summary>Gets or sets the type: Intro, Outro, Recap or Preview.</summary>
    public string? Type { get; set; }

    /// <summary>Gets or sets the start in ticks.</summary>
    public long StartTicks { get; set; }

    /// <summary>Gets or sets the end in ticks.</summary>
    public long EndTicks { get; set; }
}

/// <summary>Markers push body.</summary>
public class MarkersRequest
{
    /// <summary>Gets or sets the segments to show for the item (replaces what we stored before). Required; [] clears.</summary>
    public List<MarkerSegmentDto?>? Segments { get; set; }

    /// <summary>
    /// Gets or sets the size in bytes of the media file the markers were detected on. Optional; when set, the markers
    /// are not served while the file on disk has a different size (it was replaced).
    /// </summary>
    public long? FileSize { get; set; }
}

/// <summary>Stores Skip Intro / Skip Credits markers pushed by Media Preview Generator and publishes them as media segments.</summary>
[ApiController]
[Authorize(Policy = "RequiresElevation")]
[Route("MediaPreviewBridge/Markers")]
[Produces(MediaTypeNames.Application.Json)]
public class MarkersController : ControllerBase
{
    /// <summary>Most segments one item can carry; far above the four types, low enough to bound the store file.</summary>
    public const int MaxSegments = 64;

    // Exact names only: Enum.TryParse would also accept "5" or "Intro,Outro" (flag-style OR) and turn them into a type.
    private static readonly Dictionary<string, MediaSegmentType> TypesByName =
        MarkerStore.AllowedTypes.ToDictionary(t => t.ToString(), StringComparer.OrdinalIgnoreCase);

    private readonly ILibraryManager _libraryManager;
    private readonly IMediaSegmentManager _segmentManager;
    private readonly ILogger<MarkersController> _logger;

    /// <summary>Initializes a new instance of the <see cref="MarkersController"/> class.</summary>
    /// <param name="libraryManager">Library manager.</param>
    /// <param name="segmentManager">Media segment manager.</param>
    /// <param name="logger">Logger.</param>
    public MarkersController(ILibraryManager libraryManager, IMediaSegmentManager segmentManager, ILogger<MarkersController> logger)
    {
        _libraryManager = libraryManager;
        _segmentManager = segmentManager;
        _logger = logger;
    }

    /// <summary>Stored markers for an item.</summary>
    /// <param name="itemId">Item id.</param>
    /// <returns>Stored segments, the file size they were detected on, and whether that no longer matches the file.</returns>
    [HttpGet("{itemId:guid}")]
    public IActionResult Get([FromRoute] Guid itemId)
    {
        var item = _libraryManager.GetItemById(itemId);
        if (item is null)
        {
            return NotFound(new { error = "item not found" });
        }

        var markers = MarkerStore.Load(itemId, _logger);
        var stale = markers.IsStale(BridgeSegmentProvider.CurrentFileSize(item));
        var segments = markers.Segments.Select(s => new { type = s.Type.ToString(), startTicks = s.StartTicks, endTicks = s.EndTicks });
        return Ok(new { itemId, fileSize = markers.FileSize, stale, segments });
    }

    /// <summary>Replace the markers for an item and publish them.</summary>
    /// <param name="itemId">Item id.</param>
    /// <param name="request">Segments and the optional file size.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>Count stored.</returns>
    [HttpPost("{itemId:guid}")]
    public async Task<IActionResult> Post([FromRoute] Guid itemId, [FromBody] MarkersRequest? request, CancellationToken cancellationToken)
    {
        var item = _libraryManager.GetItemById(itemId);
        if (item is null)
        {
            return NotFound(new { error = "item not found" });
        }

        if (item is not Video)
        {
            return BadRequest(new { error = "item is not a video" });
        }

        // A missing list is a caller bug, not a request to clear: only an explicit [] clears.
        if (request?.Segments is null)
        {
            return BadRequest(new { error = "segments is required" });
        }

        if (request.FileSize is <= 0)
        {
            return BadRequest(new { error = $"invalid fileSize {request.FileSize}" });
        }

        if (request.Segments.Count > MaxSegments)
        {
            return BadRequest(new { error = $"too many segments ({request.Segments.Count}, max {MaxSegments})" });
        }

        var stored = new List<StoredSegment>();
        foreach (var s in request.Segments)
        {
            if (s is null)
            {
                return BadRequest(new { error = "segment is null" });
            }

            if (s.Type is null || !TypesByName.TryGetValue(s.Type, out var type))
            {
                return BadRequest(new { error = $"unsupported segment type '{s.Type}'" });
            }

            var segment = new StoredSegment(type, s.StartTicks, s.EndTicks);
            if (!MarkerStore.IsValid(segment))
            {
                return BadRequest(new { error = $"invalid ticks {s.StartTicks}..{s.EndTicks}" });
            }

            stored.Add(segment);
        }

        MarkerStore.Save(itemId, new StoredMarkers(request.FileSize, stored));
        // forceOverwrite:false replaces only this provider's rows when they changed, leaving other providers' segments alone.
        await _segmentManager.RunSegmentPluginProviders(item, _libraryManager.GetLibraryOptions(item), false, cancellationToken).ConfigureAwait(false);
        _logger.LogInformation("Media Preview Bridge stored {Count} marker segment(s) for {ItemId}", stored.Count, itemId);
        return Ok(new { itemId, stored = stored.Count });
    }

    /// <summary>Remove our markers for an item.</summary>
    /// <param name="itemId">Item id.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>204.</returns>
    [HttpDelete("{itemId:guid}")]
    public async Task<IActionResult> Delete([FromRoute] Guid itemId, CancellationToken cancellationToken)
    {
        var item = _libraryManager.GetItemById(itemId);
        if (item is null)
        {
            return NotFound(new { error = "item not found" });
        }

        MarkerStore.Delete(itemId);
        // With nothing stored the provider returns no segments, which makes Jellyfin delete this provider's rows.
        await _segmentManager.RunSegmentPluginProviders(item, _libraryManager.GetLibraryOptions(item), false, cancellationToken).ConfigureAwait(false);
        return NoContent();
    }
}
