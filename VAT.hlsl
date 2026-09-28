// VAT.hlsl - Custom Function node para Shader Graph (URP / HDRP).
//
// Crea un Custom Function node -> Type: File -> Source: este archivo -> Name: VAT
// Inputs (en este orden):
//   PosTex (Texture 2D)   NrmTex (Texture 2D)   UV2 (Vector 2, canal UV1 del mesh)
//   Frame (Float)  = (Time * Fps * Speed + Phase)   <- calculalo en el grafo
//   Frames (Float)  TexW (Float)  Tiles (Float)  OffsetMode (Float, 0/1)
//   VertexPos (Vector 3, nodo Position en Object space)
// Outputs: Position (Vector 3), Normal (Vector 3) -> a Vertex Position / Vertex Normal (Object space)
// Texturas: Filter = Point, sRGB off, sin mipmaps, sin compresion.
#ifndef VAT_INCLUDED
#define VAT_INCLUDED

void VAT_float(UnityTexture2D PosTex, UnityTexture2D NrmTex, float2 UV2,
               float Frame, float Frames, float TexW, float Tiles, float OffsetMode,
               float3 VertexPos, out float3 Position, out float3 Normal)
{
#ifdef SHADERGRAPH_PREVIEW
    Position = VertexPos;
    Normal = float3(0, 1, 0);
#else
    float f  = fmod(Frame, Frames);
    float f0 = floor(f);
    float f1 = fmod(f0 + 1.0, Frames);
    float t  = f - f0;

    float tile = floor(UV2.x * Tiles + 0.5);
    float2 uvA = float2((tile * Frames + f0 + 0.5) / TexW, UV2.y);
    float2 uvB = float2((tile * Frames + f1 + 0.5) / TexW, UV2.y);

    float3 pA = SAMPLE_TEXTURE2D_LOD(PosTex.tex, PosTex.samplerstate, uvA, 0).xyz;
    float3 pB = SAMPLE_TEXTURE2D_LOD(PosTex.tex, PosTex.samplerstate, uvB, 0).xyz;
    float3 nA = SAMPLE_TEXTURE2D_LOD(NrmTex.tex, NrmTex.samplerstate, uvA, 0).xyz;
    float3 nB = SAMPLE_TEXTURE2D_LOD(NrmTex.tex, NrmTex.samplerstate, uvB, 0).xyz;

    float3 p = lerp(pA, pB, t);
    Position = (OffsetMode > 0.5) ? VertexPos + p : p;
    Normal   = normalize(lerp(nA, nB, t));
#endif
}
#endif
