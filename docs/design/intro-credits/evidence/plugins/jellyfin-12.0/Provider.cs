using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.MediaSegments;
using MediaBrowser.Model;
using MediaBrowser.Model.MediaSegments;
using Microsoft.Extensions.Logging;
namespace MarkersLab;
public class Provider(ILogger<Provider> log) : IMediaSegmentProvider
{
    public string Name => "Markers Lab";
    public Task<IReadOnlyList<MediaSegmentDto>> GetMediaSegments(MediaSegmentGenerationRequest request, CancellationToken ct)
    {
        var segs = Store.Load(request.ItemId);
        log.LogInformation("MarkersLab provider called for {Item}: existing={Existing} stored={Stored}", request.ItemId, request.ExistingSegments.Count, segs.Count);
        IReadOnlyList<MediaSegmentDto> r = segs.Select(s => new MediaSegmentDto { ItemId = request.ItemId, Type = s.Type, StartTicks = s.StartTicks, EndTicks = s.EndTicks }).ToList();
        return Task.FromResult(r);
    }
    public ValueTask<bool> Supports(BaseItem item) => ValueTask.FromResult(item is Video);
    public Task CleanupExtractedData(System.Guid itemId, CancellationToken cancellationToken) => Task.CompletedTask;
}
