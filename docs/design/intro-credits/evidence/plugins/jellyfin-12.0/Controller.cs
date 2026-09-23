using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaSegments;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
namespace MarkersLab;
[ApiController]
[Authorize(Policy = "RequiresElevation")]
[Route("MarkersLab")]
public class LabController(ILibraryManager lib, IMediaSegmentManager segs) : ControllerBase
{
    [HttpPost("Markers/{itemId:guid}")]
    public async Task<IActionResult> Put([FromRoute] Guid itemId, [FromBody] List<StoredSegment> body, CancellationToken ct)
    {
        var item = lib.GetItemById(itemId);
        if (item is null) return NotFound();
        Store.Save(itemId, body);
        await segs.RunSegmentPluginProviders(item, lib.GetLibraryOptions(item), false, ct).ConfigureAwait(false);
        return Ok(new { itemId, stored = body.Count });
    }
    [HttpDelete("Markers/{itemId:guid}")]
    public async Task<IActionResult> Del([FromRoute] Guid itemId, CancellationToken ct)
    {
        var item = lib.GetItemById(itemId);
        if (item is null) return NotFound();
        Store.Delete(itemId);
        await segs.RunSegmentPluginProviders(item, lib.GetLibraryOptions(item), false, ct).ConfigureAwait(false);
        return NoContent();
    }
}
