import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../human_body_prior/src"))
import numpy as np
import joblib 
import trimesh  
import json 

import random 

import torch
from torch.utils.data import Dataset

import pytorch3d.transforms as transforms 

from bps_torch.bps import bps_torch
from bps_torch.tools import sample_sphere_uniform

from human_body_prior.body_model.body_model import BodyModel

from manip.lafan1.utils import rotate_at_frame_w_obj 

SMPLH_PATH = os.path.join(os.path.dirname(__file__), "../../data/processed_data/smpl_all_models/smplh_amass")

def to_tensor(array, dtype=torch.float32):
    if not torch.is_tensor(array):
        array = torch.tensor(array)
    return array.to(dtype)

def rotate(points, R):
    shape = list(points.shape)
    points = to_tensor(points)
    R = to_tensor(R)
    if len(shape)>3:
        points = points.squeeze()
    if len(shape)<3:
        points = points.unsqueeze(dim=1)
    if R.shape[0] > shape[0]:
        shape[0] = R.shape[0]
    r_points = torch.matmul(points, R.transpose(1,2))
    return r_points.reshape(shape)

def get_smpl_parents(use_joints24=True):
    bm_path = os.path.join(SMPLH_PATH, 'male/model.npz')
    npz_data = np.load(bm_path)
    ori_kintree_table = npz_data['kintree_table'] # 2 X 52 

    if use_joints24:
        parents = ori_kintree_table[0, :23] # 23 
        parents[0] = -1 # Assign -1 for the root joint's parent idx.

        parents_list = parents.tolist()
        parents_list.append(ori_kintree_table[0][37])
        parents = np.asarray(parents_list) # 24 
    else:
        parents = ori_kintree_table[0, :22] # 22 
        parents[0] = -1 # Assign -1 for the root joint's parent idx.
    
    return parents

def local2global_pose(local_pose):
    # local_pose: T X J X 3 X 3 
    kintree = get_smpl_parents(use_joints24=False) 

    bs = local_pose.shape[0]

    local_pose = local_pose.view(bs, -1, 3, 3)

    global_pose = local_pose.clone()

    for jId in range(len(kintree)):
        parent_id = kintree[jId]
        if parent_id >= 0:
            global_pose[:, jId] = torch.matmul(global_pose[:, parent_id], global_pose[:, jId])

    return global_pose # T X J X 3 X 3 

def quat_ik_torch(grot_mat):
    # grot: T X J X 3 X 3 
    parents = get_smpl_parents(use_joints24=False) 

    grot = transforms.matrix_to_quaternion(grot_mat) # T X J X 4 

    res = torch.cat(
            [
                grot[..., :1, :],
                transforms.quaternion_multiply(transforms.quaternion_invert(grot[..., parents[1:], :]), \
                grot[..., 1:, :]),
            ],
            dim=-2) # T X J X 4 

    res_mat = transforms.quaternion_to_matrix(res) # T X J X 3 X 3 

    return res_mat 

def quat_fk_torch(lrot_mat, lpos, use_joints24=True):
    # lrot: N X J X 3 X 3 (local rotation with reprect to its parent joint)
    # lpos: N X J/(J+2) X 3 (root joint is in global space, the other joints are offsets relative to its parent in rest pose)
    if use_joints24:
        parents = get_smpl_parents(use_joints24=True)
    else:
        parents = get_smpl_parents() 

    lrot = transforms.matrix_to_quaternion(lrot_mat)

    gp, gr = [lpos[..., :1, :]], [lrot[..., :1, :]]
    for i in range(1, len(parents)):
        gp.append(
            transforms.quaternion_apply(gr[parents[i]], lpos[..., i : i + 1, :]) + gp[parents[i]]
        )
        if i < lrot.shape[-2]:
            gr.append(transforms.quaternion_multiply(gr[parents[i]], lrot[..., i : i + 1, :]))

    res = torch.cat(gr, dim=-2), torch.cat(gp, dim=-2)

    return res

class CanoObjectTrajDataset(Dataset):
    def __init__(
        self,
        train,
        data_root_folder,
        window=120,
        use_object_splits=False,
        input_language_condition=False,
        use_random_frame_bps=False, 
        use_object_keypoints=False, 
    ):
        self.train = train
        
        self.window = window 

        self.use_object_splits = use_object_splits 
        
        self.train_objects = ["largetable", "woodchair", "plasticbox", "largebox", "smallbox", "trashcan", "monitor", \
                    "floorlamp", "clothesstand"] 
        self.test_objects = ["smalltable", "whitechair", "suitcase", "tripod"]

        self.input_language_condition = input_language_condition 

        self.use_random_frame_bps = use_random_frame_bps 

        self.use_object_keypoints = use_object_keypoints 

        self.parents = get_smpl_parents() # 24/22 

        self.data_root_folder = data_root_folder 
        self.obj_geo_root_folder = os.path.join(self.data_root_folder, "captured_objects")
        
        self.rest_object_geo_folder = os.path.join(self.data_root_folder, "rest_object_geo")
        if not os.path.exists(self.rest_object_geo_folder):
            os.makedirs(self.rest_object_geo_folder)

        self.bps_path = "./bps.pt"

        self.language_anno_folder = os.path.join(self.data_root_folder, "omomo_text_anno_json_data") 
        
        self.contact_npy_folder = os.path.join(self.data_root_folder, "contact_labels_w_semantics_npy_files")

        train_subjects = []
        test_subjects = []
        num_subjects = 17 
        for s_idx in range(1, num_subjects+1):
            if s_idx >= 16:
                test_subjects.append("sub"+str(s_idx))
            else:
                train_subjects.append("sub"+str(s_idx))

        dest_obj_bps_npy_folder = os.path.join(self.data_root_folder, \
            "cano_object_bps_npy_files_joints24_"+str(self.window))
        dest_obj_bps_npy_folder_for_test = os.path.join(self.data_root_folder, \
            "cano_object_bps_npy_files_for_test_joints24_"+str(self.window))
    
        if not os.path.exists(dest_obj_bps_npy_folder):
            os.makedirs(dest_obj_bps_npy_folder)
        if not os.path.exists(dest_obj_bps_npy_folder_for_test):
            os.makedirs(dest_obj_bps_npy_folder_for_test)

        if self.train:
            self.dest_obj_bps_npy_folder = dest_obj_bps_npy_folder 
        else:
            self.dest_obj_bps_npy_folder = dest_obj_bps_npy_folder_for_test 

        if self.train:
            seq_data_path = os.path.join(data_root_folder, "train_diffusion_manip_seq_joints24.p")  
            processed_data_path = os.path.join(data_root_folder, \
                "cano_train_diffusion_manip_window_"+str(self.window)+"_joints24.p")   
        else:    
            seq_data_path = os.path.join(data_root_folder, "test_diffusion_manip_seq_joints24.p")
            processed_data_path = os.path.join(data_root_folder, \
                "cano_test_diffusion_manip_window_"+str(self.window)+"_joints24.p")

        min_max_mean_std_data_path = os.path.join(data_root_folder, "cano_min_max_mean_std_data_window_"+str(self.window)+"_joints24.p")
        
        self.prep_bps_data()

        if os.path.exists(processed_data_path):
            self.window_data_dict = joblib.load(processed_data_path)

            # if not self.train:
                # Mannually enable this. For testing data (discarded some testing sequences)
                # self.get_bps_from_window_data_dict()
        else:
            self.data_dict = joblib.load(seq_data_path)

            self.extract_rest_pose_object_geometry_and_rotation()

            self.cal_normalize_data_input()
            joblib.dump(self.window_data_dict, processed_data_path)            

        if os.path.exists(min_max_mean_std_data_path):
            min_max_mean_std_jpos_data = joblib.load(min_max_mean_std_data_path)
        else:
            if self.train:
                min_max_mean_std_jpos_data = self.extract_min_max_mean_std_from_data()
                joblib.dump(min_max_mean_std_jpos_data, min_max_mean_std_data_path)
           
        self.global_jpos_min = torch.from_numpy(min_max_mean_std_jpos_data['global_jpos_min']).float().reshape(24, 3)[None]
        self.global_jpos_max = torch.from_numpy(min_max_mean_std_jpos_data['global_jpos_max']).float().reshape(24, 3)[None]

        self.obj_pos_min = torch.from_numpy(min_max_mean_std_jpos_data['obj_com_pos_min']).float().reshape(1, 3)
        self.obj_pos_max = torch.from_numpy(min_max_mean_std_jpos_data['obj_com_pos_max']).float().reshape(1, 3)

        if self.use_object_splits:
            self.window_data_dict = self.filter_out_object_split()

        if self.input_language_condition:
            self.window_data_dict = self.filter_out_seq_wo_text() 

        if not self.train:
            self.window_data_dict = self.filter_out_short_sequences() 

        # Get train and validation statistics. 
        if self.train:
            print("Total number of windows for training:{0}".format(len(self.window_data_dict))) # all, Total number of windows for training:28859
        else:
            print("Total number of windows for validation:{0}".format(len(self.window_data_dict))) # all, 3224 

        # Prepare SMPLX model 
        soma_work_base_dir = os.path.join(self.data_root_folder, 'smpl_all_models')
        support_base_dir = soma_work_base_dir 
        surface_model_type = "smplx"
        surface_model_male_fname = os.path.join(support_base_dir, surface_model_type, "SMPLX_MALE.npz")
        surface_model_female_fname = os.path.join(support_base_dir, surface_model_type, "SMPLX_FEMALE.npz")
        dmpl_fname = None
        num_dmpls = None 
        num_expressions = None
        num_betas = 16 

        self.male_bm = BodyModel(bm_fname=surface_model_male_fname,
                        num_betas=num_betas,
                        num_expressions=num_expressions,
                        num_dmpls=num_dmpls,
                        dmpl_fname=dmpl_fname)
        self.female_bm = BodyModel(bm_fname=surface_model_female_fname,
                        num_betas=num_betas,
                        num_expressions=num_expressions,
                        num_dmpls=num_dmpls,
                        dmpl_fname=dmpl_fname)

        for p in self.male_bm.parameters():
            p.requires_grad = False
        for p in self.female_bm.parameters():
            p.requires_grad = False 

        self.male_bm = self.male_bm.cuda()
        self.female_bm = self.female_bm.cuda()
        
        self.bm_dict = {'male' : self.male_bm, 'female' : self.female_bm}

    def load_language_annotation(self, seq_name):
        # seq_name: sub16_clothesstand_000, etc. 
        json_path = os.path.join(self.language_anno_folder, seq_name+".json")
        json_data = json.load(open(json_path, 'r'))
        
        text_anno = json_data[seq_name]

        return text_anno 

    def filter_out_short_sequences(self):
        new_cnt = 0
        new_window_data_dict = {}
        for k in self.window_data_dict:
            window_data = self.window_data_dict[k]
            seq_name = window_data['seq_name']
           
            curr_seq_len = window_data['motion'].shape[0]

            if curr_seq_len < self.window:
                continue 

            if self.window_data_dict[k]['start_t_idx'] != 0:
                continue 

            new_window_data_dict[new_cnt] = self.window_data_dict[k]
            if "ori_w_idx" in self.window_data_dict[k]:
                new_window_data_dict[new_cnt]['ori_w_idx'] = self.window_data_dict[k]['ori_w_idx']
            else:
                new_window_data_dict[new_cnt]['ori_w_idx'] = k 
            
            new_cnt += 1

        return new_window_data_dict

    def filter_out_object_split(self):
        # Remove some sequences from window_data_dict such that we have some unseen objects during testing. 
        new_cnt = 0
        new_window_data_dict = {}
        for k in self.window_data_dict:
            window_data = self.window_data_dict[k]
            seq_name = window_data['seq_name']
            object_name = seq_name.split("_")[1]
            if self.train and object_name in self.train_objects:
                new_window_data_dict[new_cnt] = self.window_data_dict[k]
                new_window_data_dict[new_cnt]['ori_w_idx'] = k 
                new_cnt += 1

            if (not self.train) and object_name in self.test_objects:
                new_window_data_dict[new_cnt] = self.window_data_dict[k]
                new_window_data_dict[new_cnt]['ori_w_idx'] = k 
                new_cnt += 1

        return new_window_data_dict

    def filter_out_seq_wo_text(self):
        new_cnt = 0
        new_window_data_dict = {}
        for k in self.window_data_dict:
            window_data = self.window_data_dict[k]
            seq_name = window_data['seq_name']
            text_json_path = os.path.join(self.language_anno_folder, seq_name+".json")
            if os.path.exists(text_json_path):
                new_window_data_dict[new_cnt] = self.window_data_dict[k]
                if "ori_w_idx" in self.window_data_dict[k]: # Based on filtered results split by objects. 
                    new_window_data_dict[new_cnt]['ori_w_idx'] = self.window_data_dict[k]['ori_w_idx']
                else: # Based on the original window_daia_dict. 
                    new_window_data_dict[new_cnt]['ori_w_idx'] = k 
                new_cnt += 1

        return new_window_data_dict

    def apply_transformation_to_obj_geometry(self, obj_mesh_path, obj_scale, obj_rot, obj_trans):
        mesh = trimesh.load_mesh(obj_mesh_path)
        obj_mesh_verts = np.asarray(mesh.vertices) # Nv X 3
        obj_mesh_faces = np.asarray(mesh.faces) # Nf X 3 

        ori_obj_verts = torch.from_numpy(obj_mesh_verts).float()[None].repeat(obj_trans.shape[0], 1, 1) # T X Nv X 3 
    
        if torch.is_tensor(obj_scale):
            seq_scale = obj_scale.float() 
        else:
            seq_scale = torch.from_numpy(obj_scale).float() # T 
        
        if torch.is_tensor(obj_rot):
            seq_rot_mat = obj_rot.float()
        else:
            seq_rot_mat = torch.from_numpy(obj_rot).float() # T X 3 X 3 
        
        if obj_trans.shape[-1] != 1:
            if torch.is_tensor(obj_trans):
                seq_trans = obj_trans.float()[:, :, None]
            else:
                seq_trans = torch.from_numpy(obj_trans).float()[:, :, None] # T X 3 X 1 
        else:
            if torch.is_tensor(obj_trans):
                seq_trans = obj_trans.float()
            else:
                seq_trans = torch.from_numpy(obj_trans).float() # T X 3 X 1 

        transformed_obj_verts = seq_scale.unsqueeze(-1).unsqueeze(-1) * \
        seq_rot_mat.bmm(ori_obj_verts.transpose(1, 2).to(seq_trans.device)) + seq_trans
        transformed_obj_verts = transformed_obj_verts.transpose(1, 2) # T X Nv X 3 

        return transformed_obj_verts, obj_mesh_faces  

    def load_rest_pose_object_geometry(self, object_name):
        rest_obj_path = os.path.join(self.rest_object_geo_folder, object_name+".ply")
        
        mesh = trimesh.load_mesh(rest_obj_path)
        rest_verts = np.asarray(mesh.vertices) # Nv X 3
        obj_mesh_faces = np.asarray(mesh.faces) # Nf X 3

        return rest_verts, obj_mesh_faces 

    def convert_rest_pose_obj_geometry(self, object_name, obj_scale, obj_trans, obj_rot):
        # obj_scale: T, obj_trans: T X 3, obj_rot: T X 3 X 3
        # obj_mesh_verts: T X Nv X 3
        rest_obj_path = os.path.join(self.rest_object_geo_folder, object_name+".ply")
        rest_obj_json_path = os.path.join(self.rest_object_geo_folder, object_name+".json")

        if os.path.exists(rest_obj_path):
            mesh = trimesh.load_mesh(rest_obj_path)
            rest_verts = np.asarray(mesh.vertices) # Nv X 3
            obj_mesh_faces = np.asarray(mesh.faces) # Nf X 3

            rest_verts = torch.from_numpy(rest_verts) 

            json_data = json.load(open(rest_obj_json_path, 'r'))
            rest_pose_ori_obj_rot = np.asarray(json_data['rest_pose_ori_obj_rot']) # 3 X 3 
            rest_pose_ori_obj_com_pos = np.asarray(json_data['rest_pose_ori_com_pos']) # 1 X 3 
            obj_trans_to_com_pos = np.asarray(json_data['obj_trans_to_com_pos']) # 1 X 3 
        else:
            obj_mesh_verts, obj_mesh_faces = self.load_object_geometry(object_name, obj_scale, \
                                        obj_trans, obj_rot)
            com_pos = obj_mesh_verts[0].mean(dim=0)[None] # 1 X 3 
            obj_trans_to_com_pos = obj_trans[0:1] - com_pos.detach().cpu().numpy() # 1 X 3  
            tmp_verts = obj_mesh_verts[0] - com_pos # Nv X 3
            obj_rot = torch.from_numpy(obj_rot) 
            tmp_verts = tmp_verts.to(obj_rot.device)
            
            rest_verts = tmp_verts.clone() # Nv X 3 

            dest_mesh = trimesh.Trimesh(
            vertices=rest_verts.detach().cpu().numpy(),
            faces=obj_mesh_faces,
            process=False)

            result = trimesh.exchange.ply.export_ply(dest_mesh, encoding='ascii')
            output_file = open(rest_obj_path, "wb+")
            output_file.write(result)
            output_file.close()

            rest_pose_ori_obj_rot = obj_rot[0].detach().cpu().numpy() # 3 X 3
            rest_pose_ori_obj_com_pos = com_pos.detach().cpu().numpy() # 1 X 3  

            dest_data_dict = {}
            dest_data_dict['rest_pose_ori_obj_rot'] = rest_pose_ori_obj_rot.tolist() 
            dest_data_dict['rest_pose_ori_com_pos'] = rest_pose_ori_obj_com_pos.tolist()
            dest_data_dict['obj_trans_to_com_pos'] = obj_trans_to_com_pos.tolist() 

            json.dump(dest_data_dict, open(rest_obj_json_path, 'w')) 

        # Compute object's BPS representation in rest pose. 
        dest_obj_bps_npy_path = os.path.join(self.rest_object_geo_folder, object_name+".npy")

        if not os.path.exists(dest_obj_bps_npy_path):
            center_verts = torch.zeros(1, 3).to(rest_verts.device)
            object_bps = self.compute_object_geo_bps(rest_verts[None], center_verts) # 1 X 1024 X 3 
            np.save(dest_obj_bps_npy_path, object_bps.data.cpu().numpy()) 

        return rest_verts, obj_mesh_faces, rest_pose_ori_obj_rot, rest_pose_ori_obj_com_pos, obj_trans_to_com_pos  

    def load_object_geometry_w_rest_geo(self, obj_rot, obj_com_pos, rest_verts):
        # obj_scale: T, obj_rot: T X 3 X 3, obj_com_pos: T X 3, rest_verts: Nv X 3 
        rest_verts = rest_verts[None].repeat(obj_rot.shape[0], 1, 1)
        transformed_obj_verts = obj_rot.bmm(rest_verts.transpose(1, 2)) + obj_com_pos[:, :, None]
        transformed_obj_verts = transformed_obj_verts.transpose(1, 2) # T X Nv X 3 

        return transformed_obj_verts 
    
    def load_object_geometry(self, object_name, obj_scale, obj_trans, obj_rot, \
        obj_bottom_scale=None, obj_bottom_trans=None, obj_bottom_rot=None):
        obj_mesh_path = os.path.join(self.obj_geo_root_folder, \
                    object_name+"_cleaned_simplified.obj")
       
        obj_mesh_verts, obj_mesh_faces =self.apply_transformation_to_obj_geometry(obj_mesh_path, \
        obj_scale, obj_rot, obj_trans) # T X Nv X 3 

        return obj_mesh_verts, obj_mesh_faces 

    def compute_object_geo_bps(self, obj_verts, obj_trans):
        # obj_verts: T X Nv X 3, obj_trans: T X 3
        bps_object_geo = self.bps_torch.encode(x=obj_verts, \
                    feature_type=['deltas'], \
                    custom_basis=self.obj_bps.repeat(obj_trans.shape[0], \
                    1, 1)+obj_trans[:, None, :])['deltas'] # T X N X 3 

        return bps_object_geo

    def prep_bps_data(self):
        n_obj = 1024
        r_obj = 1.0 # Previous 0.6, cannot cover long objects. 
       
        # if not os.path.exists(self.bps_path):
        #     bps_obj = sample_sphere_uniform(n_points=n_obj, radius=r_obj).reshape(1, -1, 3)
          
        #     bps = {
        #         'obj': bps_obj.cpu(),
        #         # 'sbj': bps_sbj.cpu(),
        #     }
        #     torch.save(bps, self.bps_path)
        
        self.bps = torch.load(self.bps_path, weights_only=True)

        self.bps_torch = bps_torch()

        self.obj_bps = self.bps['obj']

    def extract_rest_pose_object_geometry_and_rotation(self):
        self.rest_pose_object_dict = {} 

        for seq_idx in self.data_dict:
            seq_name = self.data_dict[seq_idx]['seq_name']
            object_name = seq_name.split("_")[1]
            if object_name in ["vacuum", "mop"]:
                continue 

            if object_name not in self.rest_pose_object_dict:
                obj_trans = self.data_dict[seq_idx]['obj_trans'][:, :, 0] # T X 3
                obj_rot = self.data_dict[seq_idx]['obj_rot'] # T X 3 X 3 
                obj_scale = self.data_dict[seq_idx]['obj_scale'] # T  

                rest_verts, obj_mesh_faces, rest_pose_ori_rot, rest_pose_ori_com_pos, obj_trans_to_com_pos = \
                self.convert_rest_pose_obj_geometry(object_name, obj_scale, obj_trans, obj_rot)

                self.rest_pose_object_dict[object_name] = {}
                self.rest_pose_object_dict[object_name]['ori_rotation'] = rest_pose_ori_rot # 3 X 3 
                self.rest_pose_object_dict[object_name]['ori_trans'] = rest_pose_ori_com_pos # 1 X 3 
                self.rest_pose_object_dict[object_name]['obj_trans_to_com_pos'] = obj_trans_to_com_pos # 1 X 3 

    def cal_normalize_data_input(self):
        self.window_data_dict = {}
        s_idx = 0 
        for index in self.data_dict:
            seq_name = self.data_dict[index]['seq_name']

            object_name = seq_name.split("_")[1]

            # Skip vacuum, mop for now since they consist of two object parts. 
            if object_name in ["vacuum", "mop"]:
                continue 

            rest_pose_obj_data = self.rest_pose_object_dict[object_name]
            rest_pose_rot_mat = rest_pose_obj_data['ori_rotation'] # 3 X 3

            rest_obj_path = os.path.join(self.rest_object_geo_folder, object_name+".ply")
            mesh = trimesh.load_mesh(rest_obj_path)
            rest_verts = np.asarray(mesh.vertices) # Nv X 3
            rest_verts = torch.from_numpy(rest_verts).float() # Nv X 3

            betas = self.data_dict[index]['betas'] # 1 X 16 
            gender = self.data_dict[index]['gender']

            seq_root_trans = self.data_dict[index]['trans'] # T X 3 
            seq_root_orient = self.data_dict[index]['root_orient'] # T X 3 
            seq_pose_body = self.data_dict[index]['pose_body'].reshape(-1, 21, 3) # T X 21 X 3

            rest_human_offsets = self.data_dict[index]['rest_offsets'] # 22 X 3/24 X 3
            trans2joint = self.data_dict[index]['trans2joint'] # 3 

            # Used in old version without defining rest object geometry. 
            seq_obj_trans = self.data_dict[index]['obj_trans'][:, :, 0] # T X 3
            seq_obj_rot = self.data_dict[index]['obj_rot'] # T X 3 X 3 
            seq_obj_scale = self.data_dict[index]['obj_scale'] # T  

            seq_obj_verts, tmp_obj_faces = self.load_object_geometry(object_name, seq_obj_scale, \
                        seq_obj_trans, seq_obj_rot) # T X Nv X 3, tensor
            seq_obj_com_pos = seq_obj_verts.mean(dim=1) # T X 3 

            obj_trans = seq_obj_com_pos.clone().detach().cpu().numpy() 

            rest_pose_rot_mat_rep = torch.from_numpy(rest_pose_rot_mat).float()[None, :, :] # 1 X 3 X 3 
            obj_rot = torch.from_numpy(self.data_dict[index]['obj_rot']) # T X 3 X 3 
            obj_rot = torch.matmul(obj_rot, rest_pose_rot_mat_rep.repeat(obj_rot.shape[0], 1, 1).transpose(1, 2)) # T X 3 X 3  
            obj_rot = obj_rot.detach().cpu().numpy() 

            num_steps = seq_root_trans.shape[0]
            # for start_t_idx in range(0, num_steps, self.window//2):
            for start_t_idx in range(0, num_steps, self.window//4):
                end_t_idx = start_t_idx + self.window - 1
                
                # Skip the segment that has a length < 30 
                if end_t_idx - start_t_idx < 30:
                    continue 

                self.window_data_dict[s_idx] = {}
                
                joint_aa_rep = torch.cat((torch.from_numpy(seq_root_orient[start_t_idx:end_t_idx+1]).float()[:, None, :], \
                    torch.from_numpy(seq_pose_body[start_t_idx:end_t_idx+1]).float()), dim=1) # T X J X 3 
                X = torch.from_numpy(rest_human_offsets).float()[None].repeat(joint_aa_rep.shape[0], 1, 1).detach().cpu().numpy() # T X J X 3 
                X[:, 0, :] = seq_root_trans[start_t_idx:end_t_idx+1] 
                local_rot_mat = transforms.axis_angle_to_matrix(joint_aa_rep) # T X J X 3 X 3 
                Q = transforms.matrix_to_quaternion(local_rot_mat).detach().cpu().numpy() # T X J X 4 

                obj_x = obj_trans[start_t_idx:end_t_idx+1].copy() # T X 3 
                obj_rot_mat = torch.from_numpy(obj_rot[start_t_idx:end_t_idx+1]).float()# T X 3 X 3 
                obj_q = transforms.matrix_to_quaternion(obj_rot_mat).detach().cpu().numpy() # T X 4 

                # Canonicalize based on the first human pose's orientation. 
                X, Q, new_obj_x, new_obj_q = rotate_at_frame_w_obj(X[np.newaxis], Q[np.newaxis], \
                obj_x[np.newaxis], obj_q[np.newaxis], \
                trans2joint[np.newaxis], self.parents, n_past=1, floor_z=True)
                # 1 X T X J X 3, 1 X T X J X 4, 1 X T X 3, 1 X T X 4 

                new_seq_root_trans = X[0, :, 0, :] # T X 3 
                new_local_rot_mat = transforms.quaternion_to_matrix(torch.from_numpy(Q[0]).float()) # T X J X 3 X 3 
                new_local_aa_rep = transforms.matrix_to_axis_angle(new_local_rot_mat) # T X J X 3 
                new_seq_root_orient = new_local_aa_rep[:, 0, :] # T X 3
                new_seq_pose_body = new_local_aa_rep[:, 1:, :] # T X 21 X 3 
                
                new_obj_rot_mat = transforms.quaternion_to_matrix(torch.from_numpy(new_obj_q[0]).float()) # T X 3 X 3
                
                cano_obj_mat = torch.matmul(new_obj_rot_mat[0], obj_rot_mat[0].transpose(0, 1)) # 3 X 3 
               
                obj_verts = self.load_object_geometry_w_rest_geo(new_obj_rot_mat, \
                        torch.from_numpy(new_obj_x[0]).float().to(new_obj_rot_mat.device), rest_verts)

                center_verts = obj_verts.mean(dim=1) # T X 3 
                
                query = self.process_window_data(rest_human_offsets, trans2joint, \
                    new_seq_root_trans, new_seq_root_orient.detach().cpu().numpy(), \
                    new_seq_pose_body.detach().cpu().numpy(),  \
                    new_obj_x[0], new_obj_rot_mat.detach().cpu().numpy(), center_verts)

                # Compute BPS representation for this window
                # Save to numpy file 
                dest_obj_bps_npy_path = os.path.join(self.dest_obj_bps_npy_folder, seq_name+"_"+str(s_idx)+".npy")

                if not os.path.exists(dest_obj_bps_npy_path):
                    # object_bps = self.compute_object_geo_bps(obj_verts[0:1], center_verts[0:1]) # For the setting that only computes the first frame. 
                    object_bps = self.compute_object_geo_bps(obj_verts, center_verts) 
                    np.save(dest_obj_bps_npy_path, object_bps.data.cpu().numpy()) 

                curr_global_jpos = query['global_jpos'].detach().cpu().numpy()
                curr_global_jvel = query['global_jvel'].detach().cpu().numpy()
                curr_global_rot_6d = query['global_rot_6d'].detach().cpu().numpy()

                self.window_data_dict[s_idx]['cano_obj_mat'] = cano_obj_mat.detach().cpu().numpy() 

                self.window_data_dict[s_idx]['motion'] = np.concatenate((curr_global_jpos.reshape(-1, 24*3), \
                curr_global_jvel.reshape(-1, 24*3), curr_global_rot_6d.reshape(-1, 22*6)), axis=1) # T X (24*3+24*3+22*6)
               
                self.window_data_dict[s_idx]['seq_name'] = seq_name
                self.window_data_dict[s_idx]['start_t_idx'] = start_t_idx
                self.window_data_dict[s_idx]['end_t_idx'] = end_t_idx 

                self.window_data_dict[s_idx]['betas'] = betas 
                self.window_data_dict[s_idx]['gender'] = gender

                self.window_data_dict[s_idx]['trans2joint'] = trans2joint 

                self.window_data_dict[s_idx]['obj_rot_mat'] = query['obj_rot_mat'].detach().cpu().numpy()
    
                self.window_data_dict[s_idx]['window_obj_com_pos'] = query['window_obj_com_pos'].detach().cpu().numpy() 

                self.window_data_dict[s_idx]['rest_human_offsets'] = rest_human_offsets 

                s_idx += 1 
       
    def extract_min_max_mean_std_from_data(self):
        all_global_jpos_data = []
        all_global_jvel_data = []

        all_obj_com_pos_data = []

        for s_idx in self.window_data_dict:
            curr_window_data = self.window_data_dict[s_idx]['motion'] # T X D 
   
            all_global_jpos_data.append(curr_window_data[:, :24*3])
            all_global_jvel_data.append(curr_window_data[:, 24*3:2*24*3])
       
            curr_com_pos = self.window_data_dict[s_idx]['window_obj_com_pos'] # T X 3 

            all_obj_com_pos_data.append(curr_com_pos) 

        all_global_jpos_data = np.vstack(all_global_jpos_data).reshape(-1, 72) # (N*T) X 72 
        all_global_jvel_data = np.vstack(all_global_jvel_data).reshape(-1, 72)

        all_obj_com_pos_data = np.vstack(all_obj_com_pos_data).reshape(-1, 3) # (N*T) X 3 

        min_jpos = all_global_jpos_data.min(axis=0)
        max_jpos = all_global_jpos_data.max(axis=0)
        min_jvel = all_global_jvel_data.min(axis=0)
        max_jvel = all_global_jvel_data.max(axis=0)

        min_com_pos = all_obj_com_pos_data.min(axis=0)
        max_com_pos = all_obj_com_pos_data.max(axis=0)

        stats_dict = {}
        stats_dict['global_jpos_min'] = min_jpos 
        stats_dict['global_jpos_max'] = max_jpos 
        stats_dict['global_jvel_min'] = min_jvel 
        stats_dict['global_jvel_max'] = max_jvel  

        stats_dict['obj_com_pos_min'] = min_com_pos
        stats_dict['obj_com_pos_max'] = max_com_pos 

        return stats_dict 

    def normalize_jpos_min_max(self, ori_jpos):
        # ori_jpos: T X 22/24 X 3 
        # or BS X T X J X 3 
        if ori_jpos.dim() == 4:
            normalized_jpos = (ori_jpos - self.global_jpos_min.to(ori_jpos.device)[None])/(self.global_jpos_max.to(ori_jpos.device)[None] \
            -self.global_jpos_min.to(ori_jpos.device)[None])
        else:
            normalized_jpos = (ori_jpos - self.global_jpos_min.to(ori_jpos.device))/(self.global_jpos_max.to(ori_jpos.device)\
            -self.global_jpos_min.to(ori_jpos.device))
        normalized_jpos = normalized_jpos * 2 - 1 # [-1, 1] range 

        return normalized_jpos # (BS X) T X 22/24 X 3 

    def de_normalize_jpos_min_max(self, normalized_jpos):
        # normalized_jpos: T X 22/24 X 3 
        # or BS X T X J X 3 
        normalized_jpos = (normalized_jpos + 1) * 0.5 # [0, 1] range
        
        if normalized_jpos.dim() == 4:
            de_jpos = normalized_jpos * (self.global_jpos_max.to(normalized_jpos.device)[None]-\
            self.global_jpos_min.to(normalized_jpos.device)[None]) + self.global_jpos_min.to(normalized_jpos.device)[None]
        else:
            de_jpos = normalized_jpos * (self.global_jpos_max.to(normalized_jpos.device)-\
            self.global_jpos_min.to(normalized_jpos.device)) + self.global_jpos_min.to(normalized_jpos.device)

        return de_jpos # (BS X) T X 22/24 X 3

    def normalize_obj_pos_min_max(self, ori_obj_pos):
        # ori_jpos: T X 3 
        if ori_obj_pos.dim() == 3: # BS X T X 3 
            normalized_jpos = (ori_obj_pos - self.obj_pos_min.to(ori_obj_pos.device)[None])/(self.obj_pos_max.to(ori_obj_pos.device)[None] \
            -self.obj_pos_min.to(ori_obj_pos.device)[None])
        else:
            normalized_jpos = (ori_obj_pos - self.obj_pos_min.to(ori_obj_pos.device))/(self.obj_pos_max.to(ori_obj_pos.device)\
            -self.obj_pos_min.to(ori_obj_pos.device))

        normalized_jpos = normalized_jpos * 2 - 1 # [-1, 1] range 

        return normalized_jpos # T X 3 /BS X T X 3

    def de_normalize_obj_pos_min_max(self, normalized_obj_pos):
        normalized_obj_pos = (normalized_obj_pos + 1) * 0.5 # [0, 1] range
        if normalized_obj_pos.dim() == 3:
            de_jpos = normalized_obj_pos * (self.obj_pos_max.to(normalized_obj_pos.device)[None]-\
            self.obj_pos_min.to(normalized_obj_pos.device)[None]) + self.obj_pos_min.to(normalized_obj_pos.device)[None]
        else:
            de_jpos = normalized_obj_pos * (self.obj_pos_max.to(normalized_obj_pos.device)-\
            self.obj_pos_min.to(normalized_obj_pos.device)) + self.obj_pos_min.to(normalized_obj_pos.device)

        return de_jpos # T X 3

    def process_window_data(self, rest_human_offsets, trans2joint, \
        seq_root_trans, seq_root_orient, seq_pose_body, \
        obj_trans, obj_rot, center_verts):
        random_t_idx = 0 
        end_t_idx = seq_root_trans.shape[0] - 1

        window_root_trans = torch.from_numpy(seq_root_trans[random_t_idx:end_t_idx+1]).cuda()
        window_root_orient = torch.from_numpy(seq_root_orient[random_t_idx:end_t_idx+1]).float().cuda()
        window_pose_body  = torch.from_numpy(seq_pose_body[random_t_idx:end_t_idx+1]).float().cuda()

        window_obj_rot_mat = torch.from_numpy(obj_rot[random_t_idx:end_t_idx+1]).float().cuda() # T X 3 X 3 
        window_obj_trans = torch.from_numpy(obj_trans[random_t_idx:end_t_idx+1]).float().cuda() # T X 3

        window_center_verts = center_verts[random_t_idx:end_t_idx+1].to(window_obj_trans.device)

        # Move thr first frame's human position to zero. 
        move_to_zero_trans = window_root_trans[0:1, :].clone() # 1 X 3 
        move_to_zero_trans[:, 2] = 0 

        # Move motion and object translation to make the initial pose trans 0. 
        window_root_trans = window_root_trans - move_to_zero_trans 
        window_obj_trans = window_obj_trans - move_to_zero_trans 
        window_center_verts = window_center_verts - move_to_zero_trans 

        window_root_rot_mat = transforms.axis_angle_to_matrix(window_root_orient) # T' X 3 X 3 
        window_pose_rot_mat = transforms.axis_angle_to_matrix(window_pose_body) # T' X 21 X 3 X 3 

        # Generate global joint rotation 
        local_joint_rot_mat = torch.cat((window_root_rot_mat[:, None, :, :], window_pose_rot_mat), dim=1) # T' X 22 X 3 X 3 
        global_joint_rot_mat = local2global_pose(local_joint_rot_mat) # T' X 22 X 3 X 3 

        curr_seq_pose_aa = torch.cat((window_root_orient[:, None, :], window_pose_body), dim=1) # T' X 22 X 3/T' X 24 X 3 
        rest_human_offsets = torch.from_numpy(rest_human_offsets).float()[None] 
        curr_seq_local_jpos = rest_human_offsets.repeat(curr_seq_pose_aa.shape[0], 1, 1).cuda() # T' X 22 X 3/T' X 24 X 3  
        curr_seq_local_jpos[:, 0, :] = window_root_trans - torch.from_numpy(trans2joint).cuda()[None] # T' X 22/24 X 3 

        local_joint_rot_mat = transforms.axis_angle_to_matrix(curr_seq_pose_aa)
        _, human_jnts = quat_fk_torch(local_joint_rot_mat, curr_seq_local_jpos)

        global_jpos = human_jnts # T' X 22/24 X 3 
        global_jvel = global_jpos[1:] - global_jpos[:-1] # (T'-1) X 22/24 X 3 

        global_joint_rot_mat = local2global_pose(local_joint_rot_mat) # T' X 22 X 3 X 3 

        local_rot_6d = transforms.matrix_to_rotation_6d(local_joint_rot_mat)
        global_rot_6d = transforms.matrix_to_rotation_6d(global_joint_rot_mat)

        query = {}

        query['local_rot_mat'] = local_joint_rot_mat # T' X 22 X 3 X 3 
        query['local_rot_6d'] = local_rot_6d # T' X 22 X 6

        query['global_jpos'] = global_jpos # T' X 22/24 X 3 
        query['global_jvel'] = torch.cat((global_jvel, \
            torch.zeros(1, global_jvel.shape[1], 3).to(global_jvel.device)), dim=0) # T' X 22/24 X 3 
        
        query['global_rot_mat'] = global_joint_rot_mat # T' X 22 X 3 X 3 
        query['global_rot_6d'] = global_rot_6d # T' X 22 X 6

        query['obj_trans'] = window_obj_trans # T' X 3 
        query['obj_rot_mat'] = window_obj_rot_mat # T' X 3 X 3 

        query['window_obj_com_pos'] = window_center_verts # T X 3 

        return query 

    def __len__(self):
        return len(self.window_data_dict)
    
    def prep_rel_obj_rot_mat_w_reference_mat(self, obj_rot_mat, ref_rot_mat):
        # obj_rot_mat: T X 3 X 3 / BS X T X 3 X 3 
        # ref_rot_mat: BS X 1 X 3 X 3/ 1 X 3 X 3 
        if obj_rot_mat.dim() == 4:
            timesteps = obj_rot_mat.shape[1]

            init_obj_rot_mat = ref_rot_mat.repeat(1, timesteps, 1, 1) # BS X T X 3 X 3
            rel_rot_mat = torch.matmul(obj_rot_mat, init_obj_rot_mat.transpose(2, 3)) # BS X T X 3 X 3
        else:
            timesteps = obj_rot_mat.shape[0]

            # Compute relative rotation matrix with respect to the first frame's object geometry. 
            init_obj_rot_mat = ref_rot_mat.repeat(timesteps, 1, 1) # T X 3 X 3
            rel_rot_mat = torch.matmul(obj_rot_mat, init_obj_rot_mat.transpose(1, 2)) # T X 3 X 3

        return rel_rot_mat 

    def rel_rot_to_seq(self, rel_rot_mat, obj_rot_mat):
        # rel_rot_mat: BS X T X 3 X 3 
        # obj_rot_mat: BS X T X 3 X 3 (only use the first frame's rotation)
        timesteps = rel_rot_mat.shape[1]

        # Compute relative rotation matrix with respect to the first frame's object geometry. 
        init_obj_rot_mat = obj_rot_mat[:, 0:1].repeat(1, timesteps, 1, 1) # BS X T X 3 X 3
        obj_rot_mat = torch.matmul(rel_rot_mat, init_obj_rot_mat.to(rel_rot_mat.device)) 

        return obj_rot_mat 

    def get_nn_pts(self, object_name, window_obj_rot_mat, obj_com_pos):
        # window_obj_rot_mat: T X 3 X 3 
        # obj_com_pos: T X 3 
        window_obj_rot_mat = torch.from_numpy(window_obj_rot_mat).float()
        obj_com_pos = torch.from_numpy(obj_com_pos).float()

        rest_obj_bps_npy_path = os.path.join(self.rest_object_geo_folder, object_name+".npy")
        rest_obj_bps_data = np.load(rest_obj_bps_npy_path) # 1 X 1024 X 3 
        nn_pts_on_mesh = self.obj_bps + torch.from_numpy(rest_obj_bps_data).float().to(self.obj_bps.device) # 1 X 1024 X 3 
        nn_pts_on_mesh = nn_pts_on_mesh.squeeze(0) # 1024 X 3 

        # Compute point positions for each frame 
        sampled_nn_pts_on_mesh = nn_pts_on_mesh[None].repeat(window_obj_rot_mat.shape[0], 1, 1)
        transformed_obj_nn_pts = window_obj_rot_mat.bmm(sampled_nn_pts_on_mesh.transpose(1, 2)) + \
                        obj_com_pos[:, :, None]
        transformed_obj_nn_pts= transformed_obj_nn_pts.transpose(1, 2) # T X K X 3

        return transformed_obj_nn_pts 

    def __getitem__(self, index):
        # index = 0 # For debug 
        data_input = self.window_data_dict[index]['motion']
        data_input = torch.from_numpy(data_input).float()

        seq_name = self.window_data_dict[index]['seq_name'] 
        object_name = seq_name.split("_")[1]
        
        window_s_idx = self.window_data_dict[index]['start_t_idx']
        window_e_idx = self.window_data_dict[index]['end_t_idx']
        contact_npy_path = os.path.join(self.contact_npy_folder, seq_name+".npy")
        contact_npy_data = np.load(contact_npy_path) # T X 4 (lhand, rhand, lfoot, rfoot)
        contact_labels = contact_npy_data[window_s_idx:window_e_idx+1] # W 
        contact_labels = torch.from_numpy(contact_labels).float() 

        trans2joint = self.window_data_dict[index]['trans2joint'] 

        rest_human_offsets = self.window_data_dict[index]['rest_human_offsets']  

        if self.use_random_frame_bps:
            if (not self.train) or self.use_object_splits or self.input_language_condition:
                ori_w_idx = self.window_data_dict[index]['ori_w_idx']
                obj_bps_npy_path = os.path.join(self.dest_obj_bps_npy_folder, seq_name+"_"+str(ori_w_idx)+".npy") 
            else:
                obj_bps_npy_path = os.path.join(self.dest_obj_bps_npy_folder, seq_name+"_"+str(index)+".npy") 
        else:
            obj_bps_npy_path = os.path.join(self.rest_object_geo_folder, object_name+".npy")

        obj_bps_data = np.load(obj_bps_npy_path) # T X N X 3 

        if self.use_random_frame_bps:
            random_sampled_t_idx = random.sample(list(range(obj_bps_data.shape[0])), 1)[0]
            obj_bps_data = obj_bps_data[random_sampled_t_idx:random_sampled_t_idx+1] # 1 X N X 3  

        obj_bps_data = torch.from_numpy(obj_bps_data) 

        obj_com_pos = torch.from_numpy(self.window_data_dict[index]['window_obj_com_pos']).float()
      
        normalized_obj_com_pos = self.normalize_obj_pos_min_max(obj_com_pos)

        # Prepare object motion information
        window_obj_rot_mat = torch.from_numpy(self.window_data_dict[index]['obj_rot_mat']).float()

        # Prepare relative rotation
        if self.use_random_frame_bps: 
            reference_obj_rot_mat = window_obj_rot_mat[random_sampled_t_idx:random_sampled_t_idx+1]
            window_rel_obj_rot_mat = self.prep_rel_obj_rot_mat_w_reference_mat(window_obj_rot_mat, \
                            window_obj_rot_mat[random_sampled_t_idx:random_sampled_t_idx+1])

        num_joints = 24         
        normalized_jpos = self.normalize_jpos_min_max(data_input[:, :num_joints*3].reshape(-1, num_joints, 3)) # T X 22 X 3 
       
        global_joint_rot = data_input[:, 2*num_joints*3:] # T X (22*6)

        new_data_input = torch.cat((normalized_jpos.reshape(-1, num_joints*3), global_joint_rot), dim=1)
        ori_data_input = torch.cat((data_input[:, :num_joints*3], global_joint_rot), dim=1)

        # Prepare object keypoints for each frame. 
        if self.use_object_keypoints:
            # Load rest pose BPS and compute nn points on the object. 
            rest_obj_bps_npy_path = os.path.join(self.rest_object_geo_folder, object_name+".npy")
            rest_obj_bps_data = np.load(rest_obj_bps_npy_path) # 1 X 1024 X 3 
            nn_pts_on_mesh = self.obj_bps + torch.from_numpy(rest_obj_bps_data).float().to(self.obj_bps.device) # 1 X 1024 X 3 
            nn_pts_on_mesh = nn_pts_on_mesh.squeeze(0) # 1024 X 3 

            # Random sample 100 points used for training
            sampled_vidxs = random.sample(list(range(1024)), 100) 
            sampled_nn_pts_on_mesh = nn_pts_on_mesh[sampled_vidxs] # K X 3 

            rest_pose_obj_nn_pts = sampled_nn_pts_on_mesh.clone() 

            # Compute point positions for each frame 
            sampled_nn_pts_on_mesh = sampled_nn_pts_on_mesh[None].repeat(window_obj_rot_mat.shape[0], 1, 1)
            transformed_obj_nn_pts = window_obj_rot_mat.bmm(sampled_nn_pts_on_mesh.transpose(1, 2)) + \
                            obj_com_pos[:, :, None]
            transformed_obj_nn_pts= transformed_obj_nn_pts.transpose(1, 2) # T X K X 3 

            if transformed_obj_nn_pts.shape[0] < self.window:
                paded_transformed_obj_nn_pts = torch.cat((transformed_obj_nn_pts, \
                                torch.zeros(self.window-transformed_obj_nn_pts.shape[0], \
                                transformed_obj_nn_pts.shape[1], transformed_obj_nn_pts.shape[2])), dim=0)
            else:
                paded_transformed_obj_nn_pts = transformed_obj_nn_pts
               
        # Add padding. 
        actual_steps = new_data_input.shape[0]
        if actual_steps < self.window:
            paded_new_data_input = torch.cat((new_data_input, torch.zeros(self.window-actual_steps, new_data_input.shape[-1])), dim=0)
            paded_ori_data_input = torch.cat((ori_data_input, torch.zeros(self.window-actual_steps, ori_data_input.shape[-1])), dim=0)  

            paded_normalized_obj_com_pos = torch.cat((normalized_obj_com_pos, \
                torch.zeros(self.window-actual_steps, 3)), dim=0) 
            paded_obj_com_pos = torch.cat((torch.from_numpy(self.window_data_dict[index]['window_obj_com_pos']).float(), \
                torch.zeros(self.window-actual_steps, 3)), dim=0)
            
            paded_obj_rot_mat = torch.cat((window_obj_rot_mat, \
                torch.zeros(self.window-actual_steps, 3, 3)), dim=0)

            if self.use_random_frame_bps:
                paded_rel_obj_rot_mat = torch.cat((window_rel_obj_rot_mat, \
                    torch.zeros(self.window-actual_steps, 3, 3)), dim=0)

            paded_contact_labels = torch.cat((contact_labels, torch.zeros(self.window-actual_steps, 4)), dim=0)
        else:
            paded_new_data_input = new_data_input 
            paded_ori_data_input = ori_data_input 

            paded_normalized_obj_com_pos = normalized_obj_com_pos
            paded_obj_com_pos = torch.from_numpy(self.window_data_dict[index]['window_obj_com_pos']).float()
            
            paded_obj_rot_mat = window_obj_rot_mat

            if self.use_random_frame_bps:
                paded_rel_obj_rot_mat = window_rel_obj_rot_mat 

            paded_contact_labels = contact_labels 

        data_input_dict = {}
        data_input_dict['motion'] = paded_new_data_input
        data_input_dict['ori_motion'] = paded_ori_data_input 

        if self.use_random_frame_bps:
            data_input_dict['ori_obj_motion'] = torch.cat((paded_obj_com_pos, \
                                            paded_rel_obj_rot_mat.reshape(-1, 9)), dim=-1) # T X (3+9)
            data_input_dict['obj_motion'] = torch.cat((paded_normalized_obj_com_pos, \
                                                paded_rel_obj_rot_mat.reshape(-1, 9)), dim=-1) # T X (3+9)

            data_input_dict['input_obj_bps'] = obj_bps_data # 1 X 1024 X 3 
        else:
            data_input_dict['ori_obj_motion'] = torch.cat((paded_obj_com_pos, \
                                                paded_obj_rot_mat.reshape(-1, 9)), dim=-1) # T X (3+9)
            data_input_dict['obj_motion'] = torch.cat((paded_normalized_obj_com_pos, \
                                                paded_obj_rot_mat.reshape(-1, 9)), dim=-1) # T X (3+9)
            data_input_dict['input_obj_bps'] = obj_bps_data[0:1] # 1 X 1024 X 3 

        data_input_dict['obj_rot_mat'] = paded_obj_rot_mat # T X 3 X 3  
        data_input_dict['obj_com_pos'] = paded_obj_com_pos 

        data_input_dict['betas'] = self.window_data_dict[index]['betas']
        data_input_dict['gender'] = str(self.window_data_dict[index]['gender'])
       
        data_input_dict['seq_name'] = seq_name
        data_input_dict['obj_name'] = seq_name.split("_")[1]

        data_input_dict['seq_len'] = actual_steps 

        data_input_dict['trans2joint'] = trans2joint 

        data_input_dict['rest_human_offsets'] = rest_human_offsets 

        data_input_dict['contact_labels'] = paded_contact_labels.float() # T X 4 

        if self.use_random_frame_bps:
            data_input_dict['reference_obj_rot_mat']= reference_obj_rot_mat

        data_input_dict['s_idx'] = window_s_idx
        data_input_dict['e_idx'] = window_e_idx 
        
        if self.input_language_condition:
            # Load language annotation 
            seq_text_anno = self.load_language_annotation(seq_name) 
            data_input_dict['text'] = seq_text_anno # a string 

        if self.use_object_keypoints:
            data_input_dict['ori_obj_keypoints'] = paded_transformed_obj_nn_pts # T X K X 3 
            data_input_dict['rest_pose_obj_pts'] = rest_pose_obj_nn_pts # K X 3 

        return data_input_dict 
        # data_input_dict['motion']: T X (22*3+22*6) range [-1, 1]
        # data_input_dict['obj_bps]: T X N X 3 
