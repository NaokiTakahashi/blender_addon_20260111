bl_info = {
    "name": "VRM Eevee Toon Material Replacer (Smooth Ramp)",
    "author": "ChatGPT",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > VRM",
    "description": "Replace VRM/MToon-like materials with a basic Eevee toon setup (Shader to RGB + ColorRamp) with smooth ramp edge to reduce polygon-edge jaggies.",
    "category": "Material",
}

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty


# -----------------------------
# Helpers (detect base color image, alpha)
# -----------------------------
def _is_image_node(n):
    return n and n.type == 'TEX_IMAGE' and getattr(n, "image", None) is not None

def _find_principled_node(tree):
    for n in tree.nodes:
        if n.type == 'BSDF_PRINCIPLED':
            return n
    return None

def _find_upstream_image_from_socket(sock, max_depth=12, visited=None):
    if visited is None:
        visited = set()
    if sock is None or sock in visited:
        return None
    visited.add(sock)

    if not sock.is_linked:
        return None

    for link in sock.links:
        n = link.from_node
        if _is_image_node(n):
            return n
        if max_depth <= 0:
            continue
        for inp in getattr(n, "inputs", []):
            if inp.is_linked:
                found = _find_upstream_image_from_socket(inp, max_depth - 1, visited)
                if found:
                    return found
    return None

def guess_basecolor_image_node(mat):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    tree = mat.node_tree

    principled = _find_principled_node(tree)
    if principled:
        base_in = principled.inputs.get("Base Color")
        img = _find_upstream_image_from_socket(base_in)
        if img:
            return img

    keys = ("main", "base", "albedo", "color", "maintex", "diffuse")
    candidates = []
    for n in tree.nodes:
        if _is_image_node(n):
            nm = (n.name or "").lower()
            if any(k in nm for k in keys):
                candidates.append(n)
    if candidates:
        return candidates[0]

    for n in tree.nodes:
        if _is_image_node(n):
            return n

    return None

def guess_alpha_need(mat, base_img_node):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return ('NONE', None)
    tree = mat.node_tree

    try:
        if getattr(mat, "blend_method", "OPAQUE") != "OPAQUE":
            if base_img_node and base_img_node.image:
                return ('FROM_BASE', None)
    except Exception:
        pass

    for n in tree.nodes:
        if _is_image_node(n):
            nm = (n.name or "").lower()
            if ("alpha" in nm) or ("opacity" in nm) or ("transparent" in nm):
                return ('SEPARATE_IMAGE', n)

    if base_img_node and base_img_node.image:
        return ('FROM_BASE', None)

    return ('NONE', None)

def copy_uvmap_setting(src_img_node, dst_img_node):
    try:
        dst_img_node.uv_map = getattr(src_img_node, "uv_map", "")
    except Exception:
        pass


# -----------------------------
# Eevee engine helper
# -----------------------------
def ensure_eevee_engine(scene):
    try:
        engines = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items.keys()
        if "BLENDER_EEVEE_NEXT" in engines:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
        elif "BLENDER_EEVEE" in engines:
            scene.render.engine = "BLENDER_EEVEE"
    except Exception:
        pass


# -----------------------------
# Build toon material (Eevee)
# -----------------------------
def build_eevee_toon_material(
    new_mat,
    base_img_node=None,
    alpha_mode=('NONE', None),
    alpha_blend_method='HASHED',
    alpha_clip_threshold=0.5,
    ramp_center=0.62,
    ramp_softness=0.02,
    shadow_value=0.25
):
    """
    Basic toon look:
      BaseColor (Image) * Ramp(LightFactor) -> Emission -> Output

    LightFactor:
      Diffuse(white) -> ShaderToRGB -> RGBtoBW -> ColorRamp (with small transition width)
    """
    new_mat.use_nodes = True
    tree = new_mat.node_tree
    nodes = tree.nodes
    links = tree.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)

    # Base color texture
    tex = None
    if base_img_node and base_img_node.image:
        tex = nodes.new("ShaderNodeTexImage")
        tex.location = (-800, 120)
        tex.image = base_img_node.image
        tex.interpolation = getattr(base_img_node, "interpolation", 'Linear')
        tex.extension = getattr(base_img_node, "extension", 'REPEAT')
        copy_uvmap_setting(base_img_node, tex)
        try:
            tex.image.colorspace_settings.name = "sRGB"
        except Exception:
            pass

    # Lighting factor
    diffuse = nodes.new("ShaderNodeBsdfDiffuse")
    diffuse.location = (-650, -140)
    diffuse.inputs["Color"].default_value = (1, 1, 1, 1)

    shader_to_rgb = nodes.new("ShaderNodeShaderToRGB")
    shader_to_rgb.location = (-430, -140)

    rgb_to_bw = nodes.new("ShaderNodeRGBToBW")
    rgb_to_bw.location = (-250, -140)

    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.location = (-70, -140)

    # --- Smooth edge setup (reduces polygon-edge jaggies) ---
    c = max(0.0, min(1.0, ramp_center))
    s = max(0.0, min(0.2, ramp_softness))
    p0 = max(0.0, min(1.0, c - s))
    p1 = max(0.0, min(1.0, c + s))

    try:
        # Ensure exactly 2 elements
        while len(ramp.color_ramp.elements) > 2:
            ramp.color_ramp.elements.remove(ramp.color_ramp.elements[-1])
        while len(ramp.color_ramp.elements) < 2:
            ramp.color_ramp.elements.new(0.5)

        ramp.color_ramp.interpolation = 'EASE'

        ramp.color_ramp.elements[0].position = p0
        ramp.color_ramp.elements[0].color = (shadow_value, shadow_value, shadow_value, 1.0)

        ramp.color_ramp.elements[1].position = p1
        ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
    except Exception:
        pass

    links.new(diffuse.outputs["BSDF"], shader_to_rgb.inputs["Shader"])
    links.new(shader_to_rgb.outputs["Color"], rgb_to_bw.inputs["Color"])
    links.new(rgb_to_bw.outputs["Val"], ramp.inputs["Fac"])

    # Multiply base color by ramp
    mul = nodes.new("ShaderNodeMixRGB")
    mul.location = (180, 20)
    mul.blend_type = 'MULTIPLY'
    mul.inputs["Fac"].default_value = 1.0

    if tex is not None:
        links.new(tex.outputs["Color"], mul.inputs["Color1"])
    else:
        mul.inputs["Color1"].default_value = (1, 1, 1, 1)

    links.new(ramp.outputs["Color"], mul.inputs["Color2"])

    emit = nodes.new("ShaderNodeEmission")
    emit.location = (420, 0)
    links.new(mul.outputs["Color"], emit.inputs["Color"])
    emit.inputs["Strength"].default_value = 1.0

    use_alpha = (alpha_mode[0] != 'NONE')
    if use_alpha:
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        transparent.location = (420, -240)

        mix = nodes.new("ShaderNodeMixShader")
        mix.location = (650, -60)

        links.new(transparent.outputs["BSDF"], mix.inputs[1])
        links.new(emit.outputs["Emission"], mix.inputs[2])

        if alpha_mode[0] == 'FROM_BASE' and tex is not None and tex.outputs.get("Alpha") is not None:
            links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
        elif alpha_mode[0] == 'SEPARATE_IMAGE' and alpha_mode[1] is not None and getattr(alpha_mode[1], "image", None) is not None:
            a_src = alpha_mode[1]
            tex_a = nodes.new("ShaderNodeTexImage")
            tex_a.location = (-800, -40)
            tex_a.image = a_src.image
            tex_a.interpolation = getattr(a_src, "interpolation", 'Linear')
            tex_a.extension = getattr(a_src, "extension", 'REPEAT')
            copy_uvmap_setting(a_src, tex_a)
            try:
                tex_a.image.colorspace_settings.name = "Non-Color"
            except Exception:
                pass
            if tex_a.outputs.get("Alpha") is not None:
                links.new(tex_a.outputs["Alpha"], mix.inputs["Fac"])
            else:
                links.new(tex_a.outputs["Color"], mix.inputs["Fac"])
        else:
            mix.inputs["Fac"].default_value = 1.0

        links.new(mix.outputs["Shader"], out.inputs["Surface"])

        # Eevee transparency settings
        try:
            new_mat.blend_method = alpha_blend_method
            new_mat.shadow_method = alpha_blend_method if alpha_blend_method in {'HASHED', 'CLIP'} else 'HASHED'
            if alpha_blend_method == 'CLIP':
                new_mat.alpha_threshold = alpha_clip_threshold
        except Exception:
            pass

        try:
            new_mat.use_backface_culling = False
        except Exception:
            pass
    else:
        links.new(emit.outputs["Emission"], out.inputs["Surface"])
        try:
            new_mat.blend_method = 'OPAQUE'
            new_mat.shadow_method = 'OPAQUE'
        except Exception:
            pass

    return use_alpha


# -----------------------------
# Apply helpers (optional smoothing + weighted normal)
# -----------------------------
def set_shade_smooth(obj):
    if obj.type == 'MESH' and obj.data:
        try:
            for p in obj.data.polygons:
                p.use_smooth = True
        except Exception:
            pass

def ensure_weighted_normal(obj):
    if obj.type != 'MESH':
        return
    # Avoid duplicates
    for m in obj.modifiers:
        if m.type == 'WEIGHTED_NORMAL':
            return
    try:
        mod = obj.modifiers.new(name="WeightedNormal", type='WEIGHTED_NORMAL')
        # These properties may vary slightly by version; set safely
        if hasattr(mod, "keep_sharp"):
            mod.keep_sharp = True
        if hasattr(mod, "weight"):
            mod.weight = 50
    except Exception:
        pass


# -----------------------------
# Apply to objects/materials
# -----------------------------
def collect_target_objects(context, scope):
    if scope == 'SELECTED':
        return [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}]
    else:
        return [o for o in context.scene.objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}]

def convert_object_materials(
    obj, create_new, overwrite, cache_map,
    alpha_blend_method, alpha_clip_threshold,
    ramp_center, ramp_softness, shadow_value
):
    converted = 0
    slots = 0
    for slot in obj.material_slots:
        slots += 1
        old = slot.material
        if old is None:
            continue
        if old.name.startswith("VRM_TOON_") and not overwrite:
            continue

        if old in cache_map:
            slot.material = cache_map[old]
            continue

        if create_new:
            new = old.copy()
            new.name = f"VRM_TOON_{old.name}"
        else:
            if overwrite:
                new = old
            else:
                continue

        base_img = guess_basecolor_image_node(old)
        alpha_mode = guess_alpha_need(old, base_img)

        build_eevee_toon_material(
            new,
            base_img_node=base_img,
            alpha_mode=alpha_mode,
            alpha_blend_method=alpha_blend_method,
            alpha_clip_threshold=alpha_clip_threshold,
            ramp_center=ramp_center,
            ramp_softness=ramp_softness,
            shadow_value=shadow_value
        )

        cache_map[old] = new
        slot.material = new
        converted += 1

    return converted, slots


# -----------------------------
# Operator + UI Panel
# -----------------------------
class VRM_OT_replace_with_eevee_toon(bpy.types.Operator):
    bl_idname = "vrm.replace_with_eevee_toon"
    bl_label = "Replace VRM shaders -> Eevee Toon (smooth edge)"
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(
        name="Scope",
        items=[('SELECTED', "Selected objects", ""), ('SCENE', "Whole scene", "")],
        default='SELECTED'
    )
    create_new_materials: BoolProperty(
        name="Create new materials (recommended)",
        default=True
    )
    overwrite_existing: BoolProperty(
        name="Overwrite already converted materials",
        default=False
    )

    alpha_blend_method: EnumProperty(
        name="Transparency Mode (Eevee)",
        items=[
            ('HASHED', "Alpha Hashed (hair/eyelashes)", ""),
            ('CLIP', "Alpha Clip (crisp cutout)", ""),
            ('BLEND', "Alpha Blend (semi-transparent)", ""),
        ],
        default='HASHED'
    )
    alpha_clip_threshold: FloatProperty(
        name="Clip Threshold",
        default=0.5, min=0.0, max=1.0
    )

    # --- Ramp smoothing controls ---
    ramp_center: FloatProperty(
        name="Ramp Center",
        default=0.62, min=0.0, max=1.0,
        description="Where the light/shadow boundary sits."
    )
    ramp_softness: FloatProperty(
        name="Ramp Softness",
        default=0.02, min=0.0, max=0.2,
        description="Small transition width to reduce jaggies (try 0.01~0.05)."
    )
    shadow_value: FloatProperty(
        name="Shadow Brightness",
        default=0.25, min=0.0, max=1.0,
        description="Brightness for the shadow band."
    )

    # --- Optional mesh shading helpers ---
    set_engine_to_eevee: BoolProperty(
        name="Switch render engine to Eevee",
        default=True
    )
    auto_shade_smooth: BoolProperty(
        name="Shade Smooth target meshes",
        default=True,
        description="Set polygon smoothing on meshes (helps toon boundary stability)."
    )
    add_weighted_normal: BoolProperty(
        name="Add Weighted Normal modifier",
        default=True,
        description="Improves shading on curved low-poly areas (often fixes arm jaggies)."
    )

    def execute(self, context):
        if self.set_engine_to_eevee:
            ensure_eevee_engine(context.scene)

        objs = collect_target_objects(context, self.scope)
        if not objs:
            self.report({"WARNING"}, "No target objects found.")
            return {"CANCELLED"}

        # Optional mesh shading improvements
        for obj in objs:
            if self.auto_shade_smooth:
                set_shade_smooth(obj)
            if self.add_weighted_normal:
                ensure_weighted_normal(obj)

        cache_map = {}
        total_converted = 0
        total_slots = 0

        for obj in objs:
            c, s = convert_object_materials(
                obj,
                create_new=self.create_new_materials,
                overwrite=self.overwrite_existing,
                cache_map=cache_map,
                alpha_blend_method=self.alpha_blend_method,
                alpha_clip_threshold=self.alpha_clip_threshold,
                ramp_center=self.ramp_center,
                ramp_softness=self.ramp_softness,
                shadow_value=self.shadow_value
            )
            total_converted += c
            total_slots += s

        self.report({"INFO"}, f"Done. Converted slots: {total_converted}/{total_slots}, unique mats: {len(cache_map)}")
        return {"FINISHED"}


class VRM_PT_eevee_toon_panel(bpy.types.Panel):
    bl_label = "VRM Eevee Toon"
    bl_idname = "VRM_PT_eevee_toon_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VRM"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)

        col.label(text="Basic toon + smooth ramp edge")
        col.separator()

        scn = context.scene
        col.prop(scn, "vrm_toon_scope")
        col.prop(scn, "vrm_toon_create_new")
        col.prop(scn, "vrm_toon_overwrite")

        col.separator()
        col.label(text="Toon ramp")
        col.prop(scn, "vrm_toon_ramp_center")
        col.prop(scn, "vrm_toon_ramp_softness")
        col.prop(scn, "vrm_toon_shadow_value")

        col.separator()
        col.label(text="Transparency (Eevee)")
        col.prop(scn, "vrm_toon_alpha_mode")
        if scn.vrm_toon_alpha_mode == 'CLIP':
            col.prop(scn, "vrm_toon_alpha_clip_threshold")

        col.separator()
        col.label(text="Mesh helpers")
        col.prop(scn, "vrm_toon_auto_smooth")
        col.prop(scn, "vrm_toon_weighted_normal")

        col.separator()
        col.prop(scn, "vrm_toon_set_engine")

        op = col.operator(VRM_OT_replace_with_eevee_toon.bl_idname, icon="MATERIAL")
        op.scope = scn.vrm_toon_scope
        op.create_new_materials = scn.vrm_toon_create_new
        op.overwrite_existing = scn.vrm_toon_overwrite
        op.alpha_blend_method = scn.vrm_toon_alpha_mode
        op.alpha_clip_threshold = scn.vrm_toon_alpha_clip_threshold
        op.ramp_center = scn.vrm_toon_ramp_center
        op.ramp_softness = scn.vrm_toon_ramp_softness
        op.shadow_value = scn.vrm_toon_shadow_value
        op.auto_shade_smooth = scn.vrm_toon_auto_smooth
        op.add_weighted_normal = scn.vrm_toon_weighted_normal
        op.set_engine_to_eevee = scn.vrm_toon_set_engine


classes = (
    VRM_OT_replace_with_eevee_toon,
    VRM_PT_eevee_toon_panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)

    bpy.types.Scene.vrm_toon_scope = EnumProperty(
        name="Scope",
        items=[('SELECTED', "Selected objects", ""), ('SCENE', "Whole scene", "")],
        default='SELECTED'
    )
    bpy.types.Scene.vrm_toon_create_new = BoolProperty(name="Create new materials", default=True)
    bpy.types.Scene.vrm_toon_overwrite = BoolProperty(name="Overwrite converted", default=False)

    bpy.types.Scene.vrm_toon_alpha_mode = EnumProperty(
        name="Transparency Mode",
        items=[('HASHED', "Alpha Hashed", ""), ('CLIP', "Alpha Clip", ""), ('BLEND', "Alpha Blend", "")],
        default='HASHED'
    )
    bpy.types.Scene.vrm_toon_alpha_clip_threshold = FloatProperty(name="Clip Threshold", default=0.5, min=0.0, max=1.0)

    bpy.types.Scene.vrm_toon_ramp_center = FloatProperty(name="Ramp Center", default=0.62, min=0.0, max=1.0)
    bpy.types.Scene.vrm_toon_ramp_softness = FloatProperty(
        name="Ramp Softness", default=0.02, min=0.0, max=0.2
    )
    bpy.types.Scene.vrm_toon_shadow_value = FloatProperty(name="Shadow Brightness", default=0.25, min=0.0, max=1.0)

    bpy.types.Scene.vrm_toon_auto_smooth = BoolProperty(name="Shade Smooth target meshes", default=True)
    bpy.types.Scene.vrm_toon_weighted_normal = BoolProperty(name="Add Weighted Normal modifier", default=True)
    bpy.types.Scene.vrm_toon_set_engine = BoolProperty(name="Switch render engine to Eevee", default=True)

def unregister():
    del bpy.types.Scene.vrm_toon_scope
    del bpy.types.Scene.vrm_toon_create_new
    del bpy.types.Scene.vrm_toon_overwrite
    del bpy.types.Scene.vrm_toon_alpha_mode
    del bpy.types.Scene.vrm_toon_alpha_clip_threshold
    del bpy.types.Scene.vrm_toon_ramp_center
    del bpy.types.Scene.vrm_toon_ramp_softness
    del bpy.types.Scene.vrm_toon_shadow_value
    del bpy.types.Scene.vrm_toon_auto_smooth
    del bpy.types.Scene.vrm_toon_weighted_normal
    del bpy.types.Scene.vrm_toon_set_engine

    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
