using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Model;
using MediaBrowser.Model.MediaSegments;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.MediaPreviewBridge.Markers;

/// <summary>
/// Hands stored markers to Jellyfin. Jellyfin only serves segments whose provider is registered, and deletes a
/// provider's rows when it returns none — so this provider is what makes pushed markers visible and durable.
/// </summary>
public class BridgeSegmentProvider : IMediaSegmentProvider
{
    /// <summary>Provider name; Jellyfin derives the provider id from it, so never rename.</summary>
    public const string ProviderName = "Media Preview Bridge";

    private readonly ILibraryManager _libraryManager;
    private readonly ILogger<BridgeSegmentProvider> _logger;

    /// <summary>Initializes a new instance of the <see cref="BridgeSegmentProvider"/> class.</summary>
    /// <param name="libraryManager">Library manager, to find the item's media file.</param>
    /// <param name="logger">Logger.</param>
    public BridgeSegmentProvider(ILibraryManager libraryManager, ILogger<BridgeSegmentProvider> logger)
    {
        _libraryManager = libraryManager;
        _logger = logger;
    }

    /// <inheritdoc />
    public string Name => ProviderName;

    /// <summary>Size of the item's media file on disk.</summary>
    /// <param name="item">The item (may be null).</param>
    /// <returns>The size in bytes, or null when there is no readable file.</returns>
    public static long? CurrentFileSize(BaseItem? item)
    {
        if (item is null || string.IsNullOrEmpty(item.Path))
        {
            return null;
        }

        try
        {
            var info = new FileInfo(item.Path);
            return info.Exists ? info.Length : null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException or SecurityException)
        {
            return null;
        }
    }

    /// <inheritdoc />
    public Task<IReadOnlyList<MediaSegmentDto>> GetMediaSegments(MediaSegmentGenerationRequest request, CancellationToken cancellationToken)
    {
        var markers = MarkerStore.Load(request.ItemId, _logger);
        if (markers.Segments.Count == 0)
        {
            return Task.FromResult<IReadOnlyList<MediaSegmentDto>>(Array.Empty<MediaSegmentDto>());
        }

        var item = _libraryManager.GetItemById(request.ItemId);
        var currentSize = CurrentFileSize(item);
        if (markers.IsStale(currentSize))
        {
            _logger.LogDebug(
                "Media Preview Bridge: not serving markers for {ItemId}: stored for a {StoredSize}-byte file, {Path} is {CurrentSize} bytes",
                request.ItemId,
                markers.FileSize,
                item?.Path,
                currentSize);
            return Task.FromResult<IReadOnlyList<MediaSegmentDto>>(Array.Empty<MediaSegmentDto>());
        }

        IReadOnlyList<MediaSegmentDto> segments = markers.Segments
            .Select(s => new MediaSegmentDto { ItemId = request.ItemId, Type = s.Type, StartTicks = s.StartTicks, EndTicks = s.EndTicks })
            .ToList();
        return Task.FromResult(segments);
    }

    /// <inheritdoc />
    public ValueTask<bool> Supports(BaseItem item) => ValueTask.FromResult(item is Video);

#if JF12
    /// <inheritdoc />
    /// <remarks>Jellyfin 12 calls this only when a refresh finds the media file changed, so the markers no longer fit.</remarks>
    public Task CleanupExtractedData(Guid itemId, CancellationToken cancellationToken)
    {
        MarkerStore.Delete(itemId);
        return Task.CompletedTask;
    }
#endif
}
