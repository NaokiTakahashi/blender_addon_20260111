bl_info = {
    "name": "VRM Cycles Simple Shader Replacer",
    "author": "ChatGPT",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > VRM",
    "description": "Replace VRM/MToon-like materials with a simple Cycles-friendly setup (Image->Principled + Transparent Mix).",
    "category": "Material",
}

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, FloatProperty


# -----------------------------
# Utilities: traversal helpers
# -----------------------------
def _is_image_node(n):
    return n and n.type == 'TEX_IMAGE' and getattr(n, "image", None) is not None

def _is_normalmap_node(n):
    return n and n.type == 'NORMAL_MAP'

def _iter_links_to_socket(tree, to_socket):
    for lk in tree.links:
        if lk.to_socket == to_socket:
            yield lk

def _find_upstream_image_from_socket(sock, max_depth=12, visited=None):
    """Trace upstream links to find the first TEX_IMAGE node."""
    if visited is None:
        visited = set()
    if sock is None:
        return None
    if sock in visited:
        return None
    visited.add(sock)

    if not sock.is_linked:
        return None

    for link in sock.links:
        n = link.from_node
        if _is_image_node(n):
            return n
        # go deeper through common nodes
        if max_depth > 0:
            # Try to trace from a reasonable input socket of that node
            # We prefer the first linked input socket.
            for inp in getattr(n, "inputs", []):
                if inp.is_linked:
                    found = _find_upstream_image_from_socket(inp, max_depth - 1, visited)
                    if found:
                        return found
    return None

def _find_upstream_normal_image(tree):
    """Find image texture used for normal map by looking for NORMAL_MAP node inputs."""
    for n in tree.nodes:
        if _is_normalmap_node(n):
            # Normal Map node commonly takes Color input from image
            col_in = n.inputs.get("Color")
            img = _find_upstream_image_from_socket(col_in)
            if img:
                return img, n
    # Sometimes image connects directly into shader normal via Bump etc.
    # We keep it simple: no further heuristics for now.
    return None, None

def _find_principled_node(tree):
    for n in tree.nodes:
        if n.type == 'BSDF_PRINCIPLED':
            return n
    return None

def _find_material_output(tree):
    for n in tree.nodes:
        if n.type == 'OUTPUT_MATERIAL':
            return n
    return None

def _guess_basecolor_image_node(mat):
    """Try to guess the base color image texture used in this material."""
    if not mat.use_nodes or not mat.node_tree:
        return None
    tree = mat.node_tree

    # 1) Prefer image feeding into Principled Base Color if exists
    principled = _find_principled_node(tree)
    if principled:
        base_in = principled.inputs.get("Base Color")
        img = _find_upstream_image_from_socket(base_in)
        if img:
            return img

    # 2) Prefer image feeding into any shader color (Emission/BSDF etc.)
    for n in tree.nodes:
        if n.type in {'BSDF_DIFFUSE', 'BSDF_PRINCIPLED', 'EMISSION', 'BSDF_GLOSSY', 'BSDF_TOON'}:
            for key in ("Color", "Base Color"):
                s = n.inputs.get(key)
                img = _find_upstream_image_from_socket(s)
                if img:
                    return img

    # 3) Name heuristics: MainTex/BaseColor/Albedo/Color
    candidates = []
    keys = ("main", "base", "albedo", "color", "maintx", "maintex", "diffuse")
    for n in tree.nodes:
        if _is_image_node(n):
            nm = (n.name or "").lower()
            if any(k in nm for k in keys):
                candidates.append(n)
    if candidates:
        return candidates[0]

    # 4) Fallback: first image node in the tree
    for n in tree.nodes:
        if _is_image_node(n):
            return n

    return None

def _guess_alpha_source(mat, base_img_node):
    """
    Return:
      - ('FROM_BASE', None): use base image alpha output
      - ('SEPARATE_IMAGE', image_node): use separate alpha texture
      - ('NONE', None)
    """
    if not mat.use_nodes or not mat.node_tree:
        return ('NONE', None)
    tree = mat.node_tree

    # If material already suggests blending, assume it needs alpha
    # (blend_method is Eevee setting but a useful hint)
    try:
        if getattr(mat, "blend_method", "OPAQUE") != "OPAQUE":
            if base_img_node and base_img_node.image:
                return ('FROM_BASE', None)
    except Exception:
        pass

    # Look for dedicated alpha image node by name
    for n in tree.nodes:
        if _is_image_node(n):
            nm = (n.name or "").lower()
            if "alpha" in nm or "opacity" in nm or "transparent" in nm:
                return ('SEPARATE_IMAGE', n)

    # If base image has alpha channel info, use it
    if base_img_node and base_img_node.image:
        img = base_img_node.image
        # Heuristic: 32-bit images commonly include alpha
        # Note: some images may report depth=24 even if they have alpha; still OK to use alpha output.
        try:
            if getattr(img, "depth", 0) >= 32:
                return ('FROM_BASE', None)
        except Exception:
            pass

        # Even if depth check fails, base alpha may still be meaningful
        return ('FROM_BASE', None)

    return ('NONE', None)

def _copy_uvmap_setting(src_img_node, dst_img_node):
    try:
        dst_img_node.uv_map = getattr(src_img_node, "uv_map", "")
    except Exception:
        pass

def _ensure_cycles_transparency_settings(mat, use_alpha):
    # These are mainly Eevee/viewport related, but harmless and convenient.
    # Cycles transparency is done by shader mix.
    try:
        mat.blend_method = 'BLEND' if use_alpha else 'OPAQUE'
        mat.shadow_method = 'HASHED' if use_alpha else 'OPAQUE'
    except Exception:
        pass


# -----------------------------
# Core conversion
# -----------------------------
def build_simple_cycles_material(new_mat, base_img_node=None, alpha_mode=('NONE', None), normal_info=(None, None)):
    """
    Create:
      Image Texture -> Principled Base Color
      (Alpha) Image Alpha -> Mix Fac (Transparent, Principled)
      (Normal optional) Image -> Normal Map -> Principled Normal
    """
    new_mat.use_nodes = True
    tree = new_mat.node_tree
    nodes = tree.nodes
    links = tree.links

    # Clear old nodes
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (600, 0)

    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (250, 0)
    principled.inputs["Roughness"].default_value = 0.5
    principled.inputs["Metallic"].default_value = 0.0

    # Base color
    tex = None
    if base_img_node and base_img_node.image:
        tex = nodes.new("ShaderNodeTexImage")
        tex.location = (-200, 0)
        tex.image = base_img_node.image
        tex.interpolation = getattr(base_img_node, "interpolation", 'Linear')
        tex.extension = getattr(base_img_node, "extension", 'REPEAT')
        _copy_uvmap_setting(base_img_node, tex)

        # Ensure correct colorspace for base color
        try:
            tex.image.colorspace_settings.name = "sRGB"
        except Exception:
            pass

        links.new(tex.outputs.get("Color"), principled.inputs.get("Base Color"))

    # Normal (optional)
    normal_img_node, _normalmap_node = normal_info
    if normal_img_node and normal_img_node.image:
        tex_n = nodes.new("ShaderNodeTexImage")
        tex_n.location = (-200, -280)
        tex_n.image = normal_img_node.image
        tex_n.interpolation = getattr(normal_img_node, "interpolation", 'Linear')
        tex_n.extension = getattr(normal_img_node, "extension", 'REPEAT')
        _copy_uvmap_setting(normal_img_node, tex_n)

        # Normal map should be Non-Color
        try:
            tex_n.image.colorspace_settings.name = "Non-Color"
        except Exception:
            pass

        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.location = (40, -280)
        links.new(tex_n.outputs.get("Color"), nmap.inputs.get("Color"))
        links.new(nmap.outputs.get("Normal"), principled.inputs.get("Normal"))

    # Alpha handling (Cycles-safe): Mix Transparent + Principled with alpha factor
    use_alpha = (alpha_mode[0] != 'NONE')
    if use_alpha:
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        transparent.location = (250, -220)

        mix = nodes.new("ShaderNodeMixShader")
        mix.location = (420, -80)

        # Mix: Fac=alpha (0 => Transparent, 1 => Principled)
        # Shader1 = Transparent, Shader2 = Principled
        links.new(transparent.outputs.get("BSDF"), mix.inputs[1])
        links.new(principled.outputs.get("BSDF"), mix.inputs[2])

        if alpha_mode[0] == 'FROM_BASE' and tex is not None:
            links.new(tex.outputs.get("Alpha"), mix.inputs.get("Fac"))
        elif alpha_mode[0] == 'SEPARATE_IMAGE' and alpha_mode[1] is not None and getattr(alpha_mode[1], "image", None) is not None:
            aimg_src = alpha_mode[1]
            tex_a = nodes.new("ShaderNodeTexImage")
            tex_a.location = (-200, -140)
            tex_a.image = aimg_src.image
            tex_a.interpolation = getattr(aimg_src, "interpolation", 'Linear')
            tex_a.extension = getattr(aimg_src, "extension", 'REPEAT')
            _copy_uvmap_setting(aimg_src, tex_a)
            # Alpha texture is usually Non-Color
            try:
                tex_a.image.colorspace_settings.name = "Non-Color"
            except Exception:
                pass

            # Prefer Alpha output if present; else use Color->RGB to BW
            if tex_a.outputs.get("Alpha") is not None:
                links.new(tex_a.outputs.get("Alpha"), mix.inputs.get("Fac"))
            else:
                links.new(tex_a.outputs.get("Color"), mix.inputs.get("Fac"))
        else:
            # Fallback: fully opaque if alpha not found
            mix.inputs.get("Fac").default_value = 1.0

        links.new(mix.outputs.get("Shader"), out.inputs.get("Surface"))
    else:
        links.new(principled.outputs.get("BSDF"), out.inputs.get("Surface"))

    _ensure_cycles_transparency_settings(new_mat, use_alpha)

    return use_alpha


def collect_target_objects(context, scope):
    if scope == 'SELECTED':
        return [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}]
    else:
        return [o for o in context.scene.objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}]


def convert_object_materials(obj, create_new, overwrite, cache_map):
    """
    cache_map: old_mat -> new_mat (to avoid duplicates)
    Returns (converted_count, slot_count)
    """
    converted = 0
    slots = 0

    for slot in obj.material_slots:
        slots += 1
        old = slot.material
        if old is None:
            continue
        if old.name.startswith("VRM_SIMPLE_") and not overwrite:
            continue

        if old in cache_map:
            slot.material = cache_map[old]
            continue

        # Decide target material object
        if create_new:
            new = old.copy()
            new.name = f"VRM_SIMPLE_{old.name}"
        else:
            if overwrite:
                new = old
            else:
                # If not creating new and not overwriting, nothing to do
                continue

        # Ensure nodes exist
        new.use_nodes = True

        # Extract base/alpha/normal from old (not new)
        base_img = _guess_basecolor_image_node(old)
        alpha_mode = _guess_alpha_source(old, base_img)

        normal_img_node, normal_map_node = (None, None)
        if old.use_nodes and old.node_tree:
            normal_img_node, normal_map_node = _find_upstream_normal_image(old.node_tree)

        build_simple_cycles_material(new, base_img_node=base_img, alpha_mode=alpha_mode, normal_info=(normal_img_node, normal_map_node))

        cache_map[old] = new
        slot.material = new
        converted += 1

    return converted, slots


# -----------------------------
# Operator + UI
# -----------------------------
class VRM_OT_replace_with_cycles_simple(bpy.types.Operator):
    bl_idname = "vrm.replace_with_cycles_simple"
    bl_label = "Replace VRM shaders -> Cycles Simple"
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(
        name="Scope",
        items=[
            ('SELECTED', "Selected objects", ""),
            ('SCENE', "Whole scene", ""),
        ],
        default='SELECTED'
    )

    create_new_materials: BoolProperty(
        name="Create new materials (recommended)",
        default=True,
        description="Duplicate materials and assign the new ones, keeping originals intact."
    )

    overwrite_existing_simple: BoolProperty(
        name="Overwrite already converted materials",
        default=False,
        description="If a material already looks converted, overwrite it."
    )

    def execute(self, context):
        objs = collect_target_objects(context, self.scope)
        if not objs:
            self.report({"WARNING"}, "No target objects found.")
            return {"CANCELLED"}

        cache_map = {}
        total_converted = 0
        total_slots = 0

        for obj in objs:
            c, s = convert_object_materials(
                obj,
                create_new=self.create_new_materials,
                overwrite=self.overwrite_existing_simple,
                cache_map=cache_map
            )
            total_converted += c
            total_slots += s

        self.report({"INFO"}, f"Done. Converted slots: {total_converted} / slots scanned: {total_slots}, unique mats created: {len(cache_map)}")
        return {"FINISHED"}


class VRM_PT_cycles_simple_panel(bpy.types.Panel):
    bl_label = "VRM Cycles Simple"
    bl_idname = "VRM_PT_cycles_simple_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VRM"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)

        col.label(text="Replace materials for Cycles/General")
        col.separator()

        col.prop(context.scene, "vrm_simple_scope")
        col.prop(context.scene, "vrm_simple_create_new")
        col.prop(context.scene, "vrm_simple_overwrite")

        op = col.operator(VRM_OT_replace_with_cycles_simple.bl_idname, icon="MATERIAL")
        op.scope = context.scene.vrm_simple_scope
        op.create_new_materials = context.scene.vrm_simple_create_new
        op.overwrite_existing_simple = context.scene.vrm_simple_overwrite


classes = (
    VRM_OT_replace_with_cycles_simple,
    VRM_PT_cycles_simple_panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)

    bpy.types.Scene.vrm_simple_scope = EnumProperty(
        name="Scope",
        items=[
            ('SELECTED', "Selected objects", ""),
            ('SCENE', "Whole scene", ""),
        ],
        default='SELECTED'
    )
    bpy.types.Scene.vrm_simple_create_new = BoolProperty(
        name="Create new materials",
        default=True
    )
    bpy.types.Scene.vrm_simple_overwrite = BoolProperty(
        name="Overwrite converted",
        default=False
    )

def unregister():
    del bpy.types.Scene.vrm_simple_scope
    del bpy.types.Scene.vrm_simple_create_new
    del bpy.types.Scene.vrm_simple_overwrite

    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
