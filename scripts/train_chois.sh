python trainer_chois.py \
--window=120 \
--batch_size=32 \
--data_root_folder="./data/processed_data" \
--project="./chois_release_exp_output" \
--exp_name="chois_window_120_set1" \
--wandb_pj_name="108_chois_interaction_diffusion" \
--entity="lizhe" \
--input_first_human_pose \
--use_random_frame_bps \
--add_language_condition \
--use_object_keypoints \
--loss_w_feet=1 \
--loss_w_fk=0.5 \
--loss_w_obj_pts=1
